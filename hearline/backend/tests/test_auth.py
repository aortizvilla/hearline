"""Clerk login: valid tokens map to one user each; anything else is 401. Dev mode unchanged."""

import time
from datetime import timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api import auth
from app.config import Settings, get_settings
from app.database.models import User, utcnow

ISSUER = "https://hearline-test.clerk.accounts.dev"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(sub: str = "user_abc", azp: str = "https://hearline.app", ttl: int = 60,
          key=KEY) -> str:  # type: ignore[no-untyped-def]
    now = int(time.time())
    return jwt.encode({"sub": sub, "iss": ISSUER, "azp": azp, "iat": now, "exp": now + ttl},
                      key, algorithm="RS256")


@pytest.fixture
def clerk(monkeypatch: pytest.MonkeyPatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "auth_mode", "clerk")
    monkeypatch.setattr(s, "clerk_issuer", ISSUER)
    monkeypatch.setattr(s, "clerk_authorized_parties", ["https://hearline.app"])
    monkeypatch.setattr(auth, "signing_key", lambda _t, _i: KEY.public_key())


def get(client: TestClient, tok: str | None):  # type: ignore[no-untyped-def]
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    return client.get("/api/profile", headers=headers)


def test_valid_token_creates_one_user_per_login(client: TestClient, session: Session,
                                                clerk: None) -> None:
    assert get(client, token()).status_code == 200
    assert get(client, token()).status_code == 200  # same person again: same user
    assert get(client, token(sub="user_xyz")).status_code == 200
    users = session.exec(select(User)).all()
    assert sorted(u.external_id for u in users) == ["user_abc", "user_xyz"]  # type: ignore[type-var]
    assert all(u.profile is not None for u in users)  # default profile and topics


@pytest.mark.parametrize("bad", [
    None,  # no token
    token(ttl=-60),  # expired
    token(azp="https://evil.example"),  # issued for another site
    token(key=rsa.generate_private_key(public_exponent=65537, key_size=2048)),  # forged
    "not-a-jwt",
])
def test_anything_but_a_valid_token_is_401(client: TestClient, clerk: None, bad) -> None:  # type: ignore[no-untyped-def]
    assert get(client, bad).status_code == 401


def test_dev_header_is_ignored_when_clerk_is_on(client: TestClient, clerk: None) -> None:
    assert client.get("/api/profile", headers={"X-User-Id": "1"}).status_code == 401


def test_requests_keep_the_user_active(client: TestClient, session: Session) -> None:
    client.get("/api/profile")  # dev mode: the default user
    user = session.exec(select(User)).one()
    user.last_active_at = utcnow() - timedelta(days=13)
    session.add(user)
    session.commit()
    client.get("/api/profile")
    session.refresh(user)
    assert utcnow() - user.last_active_at.replace(tzinfo=utcnow().tzinfo) < timedelta(minutes=1)


def test_production_refuses_dev_settings() -> None:
    with pytest.raises(ValueError, match="AUTH_MODE=dev"):
        Settings(app_env="production", auth_mode="dev", audio_storage="s3",
                 database_url="postgresql+psycopg://h/db")
    ok = Settings(app_env="production", auth_mode="clerk", audio_storage="s3",
                  database_url="postgresql+psycopg://h/db", llm_provider="openai",
                  tts_provider="elevenlabs")
    assert ok.auth_mode == "clerk"


@pytest.mark.parametrize("llm, tts, refused", [
    ("mock", "elevenlabs", "LLM_PROVIDER=mock"),
    ("openai", "mock", "TTS_PROVIDER=mock"),
    ("openai", "", "TTS_PROVIDER=(unset"),
])
def test_production_refuses_offline_fakes(llm: str, tts: str, refused: str) -> None:
    """A mock LLM or voice (or no voice set: pipeline.yaml defaults to mock) never goes live."""
    with pytest.raises(ValueError, match=refused.replace("(", r"\(")):
        Settings(app_env="production", auth_mode="clerk", audio_storage="s3",
                 database_url="postgresql+psycopg://h/db", llm_provider=llm, tts_provider=tts)
