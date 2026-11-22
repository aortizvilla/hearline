"""Subscription plans (config/plans.yaml): limits enforced, downgrades fitted, history kept."""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.config import plans_cfg
from app.database.models import Profile, Subscription, User, UserTopic
from app.pipeline.step4_assemble import target_minutes


@pytest.fixture
def free(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plans_cfg(), "default", "free")  # conftest signs users up on Pro


def test_new_user_starts_on_free_with_a_default_profile_that_fits(
        client: TestClient, session: Session, free: None) -> None:
    profile = client.get("/api/profile").json()
    limits = plans_cfg().plans["free"]
    assert profile["plan"] == "free"
    assert len(profile["topics"]) <= limits.max_topics  # type: ignore[operator]
    assert all(t["target_minutes"] <= limits.max_topic_minutes for t in profile["topics"])
    assert profile["voice"] in limits.voices and profile["tone"] in limits.tones

    user = session.exec(select(User)).one()
    [sub] = user.subscriptions
    assert (sub.plan, sub.status, sub.source, sub.ended_at) == ("free", "active", "signup", None)


@pytest.mark.parametrize("change, message", [
    ({"topics": [{"topic": t, "frequency": "daily", "target_minutes": 5}
                 for t in ("music", "food", "science")]}, "up to 2 topics"),
    ({"topics": [{"topic": "music", "frequency": "daily", "target_minutes": 10}]},
     "5 minutes per topic"),
    ({"topics": [{"topic": "film", "frequency": "daily", "target_minutes": 5,
                  "subtopics": ["Cannes Festival"]}]}, "Subcategories"),
    ({"voice": "not-aria"}, "voice"),
    ({"tone": "formal"}, "tone"),
])
def test_free_plan_limits_are_enforced(client: TestClient, free: None, change: dict,
                                       message: str) -> None:
    profile = {**client.get("/api/profile").json(), **change}
    if "voice" in change:  # a real voice from the catalog that Free doesn't include
        voices = [v["value"] for v in client.get("/api/options").json()["voices"]]
        profile["voice"] = next(v for v in voices if v not in plans_cfg().plans["free"].voices)
    resp = client.put("/api/profile", json=profile)
    assert resp.status_code == 403 and message in resp.json()["detail"]


def test_upgrade_unlocks_and_downgrade_trims_settings(
        client: TestClient, session: Session, free: None) -> None:
    client.get("/api/profile")
    assert client.put("/api/plan", json={"plan": "pro"}).json()["plan"] == "pro"

    voices = [v["value"] for v in client.get("/api/options").json()["voices"]]
    profile = client.get("/api/profile").json()
    paid_voice = next(v for v in voices if v not in plans_cfg().plans["free"].voices)
    profile.update(tone="formal", voice=paid_voice, topics=[
        {"topic": t, "frequency": "daily", "target_minutes": 15}
        for t in ("music", "food", "science", "world", "film")])
    shown = [t["topic"] for t in client.put("/api/profile", json=profile).json()["topics"]]

    down = client.put("/api/plan", json={"plan": "free"}).json()
    assert down["plan"] == "free"
    free_topics = plans_cfg().plans["free"].max_topics
    assert [t["topic"] for t in down["topics"]] == shown[:free_topics]  # first ones shown kept
    assert {t["target_minutes"] for t in down["topics"]} == {5}
    assert (down["voice"], down["tone"]) == ("aria", "conversational")

    user = session.exec(select(User)).one()
    history = session.exec(select(Subscription).where(Subscription.user_id == user.id)
                           .order_by(Subscription.id)).all()  # type: ignore[arg-type]
    assert [(s.plan, s.status) for s in history] == [
        ("free", "ended"), ("pro", "ended"), ("free", "active")]
    assert history[1].price_usd_month == plans_cfg().plans["pro"].price_usd_month
    assert all(s.ended_at for s in history[:2])


def test_choosing_the_same_plan_adds_no_history(client: TestClient, session: Session,
                                                free: None) -> None:
    client.get("/api/profile")
    client.put("/api/plan", json={"plan": "free"})
    assert len(session.exec(select(Subscription)).all()) == 1


def test_unknown_plan_and_closed_self_serve_are_refused(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert client.put("/api/plan", json={"plan": "gold"}).status_code == 422
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "app_env", "production")
    assert client.get("/api/plans").json()["self_serve"] is False
    assert client.put("/api/plan", json={"plan": "plus"}).status_code == 403


def test_admin_can_change_a_users_plan(client: TestClient, session: Session, free: None) -> None:
    client.get("/api/profile")
    user = session.exec(select(User)).one()
    resp = client.put(f"/api/admin/users/{user.id}/plan", json={"plan": "plus"})
    assert resp.json() == {"user_id": user.id, "plan": "plus"}
    assert session.exec(select(Subscription).where(
        Subscription.status == "active")).one().source == "admin"


def test_plans_endpoint_lists_three_plans_in_order(client: TestClient) -> None:
    plans = client.get("/api/plans").json()["plans"]
    assert [p["key"] for p in plans] == ["free", "plus", "pro"]
    assert plans[0]["price_usd_month"] == 0


def test_episode_length_is_capped_by_the_plan() -> None:
    subs = {i: UserTopic(user_id=1, topic_id=i, target_minutes=10) for i in range(4)}
    assert target_minutes(subs, Profile(user_id=1), plan_cap=15) == 15  # type: ignore[arg-type]
    assert target_minutes(subs, Profile(user_id=1)) == 30  # type: ignore[arg-type]
