"""Account features: the per-listener limit on LLM classification, account deletion (GDPR), the
Clerk webhook, and name/email owned by the login account when Clerk is on."""

import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api import auth
from app.api.webhooks import verify_svix
from app.config import get_settings, pipeline_cfg
from app.database.models import (
    CostEvent,
    Episode,
    Profile,
    Subscription,
    User,
    UserTopic,
)
from tests.test_auth import ISSUER, KEY, token

FILM = {"topic": "film", "frequency": "daily", "target_minutes": 10}


def save(client: TestClient, subtopics: list[str]):  # type: ignore[no-untyped-def]
    profile = client.get("/api/profile").json()
    return client.put(
        "/api/profile", json={**profile, "topics": [{**FILM, "subtopics": subtopics}]}
    )


# ---------- 1. the daily allowance for new phrases ----------


def test_new_phrases_stop_at_the_daily_allowance(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline_cfg().topics, "max_new_phrases_per_day", 1)
    assert save(client, ["Oscars"]).status_code == 200  # one LLM call: allowed
    used = session.exec(select(CostEvent).where(CostEvent.stage == "subtopic_resolve")).all()
    assert used and all(e.ref_id.startswith("user:") for e in used)
    resp = save(client, ["Oscars", "Silent film restorations"])  # a second new phrase
    assert resp.status_code == 429 and "today" in resp.json()["detail"]
    assert save(client, ["Oscars"]).status_code == 200  # seen before: free


def test_no_new_phrases_once_the_daily_budget_is_spent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline_cfg().scheduler, "daily_budget_usd", 0.0)
    resp = save(client, ["Silent film restorations"])
    assert resp.status_code == 429 and "right now" in resp.json()["detail"]
    assert save(client, []).status_code == 200  # catalog choices still save


# ---------- 3. account deletion ----------


def test_deleting_an_account_removes_everything_that_is_theirs(
    client: TestClient, session: Session
) -> None:
    client.get("/api/profile")  # the dev user, with defaults
    me = session.exec(select(User)).one()
    other = User(name="Other", email="other@example.com")
    session.add(other)
    session.commit()
    audio = get_settings().audio_dir / "users" / str(me.id) / "episodes" / "1.mp3"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"ID3")
    session.add(
        Episode(
            user_id=me.id,
            topic="briefing",
            title="t",
            description="",  # type: ignore[arg-type]
            status="ready",
            audio_path=f"users/{me.id}/episodes/1.mp3",
        )
    )
    session.commit()
    me_id = me.id

    resp = client.delete("/api/account")
    assert resp.status_code == 200 and resp.json()["deleted"] is True
    session.expire_all()
    assert session.get(User, me_id) is None and not audio.exists()
    for table in (Episode, UserTopic, Profile, Subscription):
        assert not session.exec(select(table).where(table.user_id == me_id)).all()  # type: ignore[attr-defined]
    assert session.get(User, other.id) is not None  # nobody else touched


# ---------- 4 + webhook: the login account owns name and email ----------

SECRET = "whsec_" + base64.b64encode(b"test-webhook-secret-32-bytes-long").decode()


def signed(event: dict, secret: str = SECRET, ts: int | None = None) -> tuple[bytes, dict]:
    body, ts = json.dumps(event).encode(), ts or int(time.time())
    key = base64.b64decode(secret.split("_", 1)[1])
    sig = base64.b64encode(hmac.new(key, f"msg_1.{ts}.".encode() + body, hashlib.sha256).digest())
    return body, {
        "svix-id": "msg_1",
        "svix-timestamp": str(ts),
        "svix-signature": f"v1,{sig.decode()}",
    }


def clerk_user(uid: str, first: str, email: str) -> dict:
    return {
        "id": uid,
        "first_name": first,
        "last_name": "Ruiz",
        "primary_email_address_id": "e1",
        "email_addresses": [{"id": "e1", "email_address": email}],
    }


@pytest.fixture
def webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "clerk_webhook_secret", SECRET)


def test_webhook_signature_checks() -> None:
    body, h = signed({"type": "x"})
    args = (SECRET, h["svix-id"], h["svix-timestamp"])
    assert verify_svix(*args, body, h["svix-signature"])
    assert not verify_svix(*args, body + b" ", h["svix-signature"])  # tampered
    assert not verify_svix(
        SECRET, "msg_1", str(int(time.time()) - 3600), body, h["svix-signature"]
    )  # stale


def test_webhook_needs_a_secret_and_a_valid_signature(
    client: TestClient, webhook: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    body, h = signed({"type": "user.created", "data": clerk_user("user_1", "Ana", "ana@x.com")})
    assert (
        client.post(
            "/api/webhooks/clerk", content=body, headers={**h, "svix-signature": "v1,bad"}
        ).status_code
        == 401
    )
    monkeypatch.setattr(get_settings(), "clerk_webhook_secret", "")
    assert client.post("/api/webhooks/clerk", content=body, headers=h).status_code == 404


def test_webhook_creates_syncs_and_erases(
    client: TestClient, session: Session, webhook: None
) -> None:
    def send(kind: str, data: dict) -> None:
        body, h = signed({"type": kind, "data": data})
        assert client.post("/api/webhooks/clerk", content=body, headers=h).status_code == 200

    send("user.created", clerk_user("user_1", "Ana", "ana@x.com"))
    user = session.exec(select(User).where(User.external_id == "user_1")).one()
    assert (user.name, user.email) == ("Ana Ruiz", "ana@x.com") and user.profile is not None
    send("user.updated", clerk_user("user_1", "Anabel", "anabel@x.com"))
    session.refresh(user)
    assert (user.name, user.email) == ("Anabel Ruiz", "anabel@x.com")
    send("user.deleted", {"id": "user_1", "deleted": True})
    session.expire_all()
    assert not session.exec(select(User).where(User.external_id == "user_1")).all()


def test_with_clerk_the_login_account_owns_name_and_email(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "auth_mode", "clerk")
    monkeypatch.setattr(s, "clerk_issuer", ISSUER)
    monkeypatch.setattr(s, "clerk_authorized_parties", [])
    monkeypatch.setattr(auth, "signing_key", lambda _t, _i: KEY.public_key())
    real = auth.verify_claims
    monkeypatch.setattr(
        "app.api.routes.verify_claims",
        lambda t: {**real(t), "name": "Ana Ruiz", "email": "ana@x.com"},
    )
    h = {"Authorization": f"Bearer {token()}"}
    profile = client.get("/api/profile", headers=h).json()
    assert (profile["name"], profile["email"]) == ("Ana Ruiz", "ana@x.com")  # from the token
    client.put(
        "/api/profile", headers=h, json={**profile, "name": "Someone else", "email": "x@y.z"}
    )
    after = client.get("/api/profile", headers=h).json()
    assert (after["name"], after["email"]) == ("Ana Ruiz", "ana@x.com")  # edits ignored
