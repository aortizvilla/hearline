"""API tests for profile, topics and episodes."""

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.database.models import Episode, User


def test_profile_defaults_and_per_topic_update(client: TestClient) -> None:
    profile = client.get("/api/profile").json()
    assert {t["topic"] for t in profile["topics"]} == {"technology", "science", "world"}

    profile["name"] = "Ana Ruiz"
    profile["topics"] = [
        {"topic": "music", "frequency": "weekly", "target_minutes": 20},
        {"topic": "food", "frequency": "daily", "target_minutes": 5},
    ]
    assert client.put("/api/profile", json=profile).status_code == 200

    saved = client.get("/api/profile").json()
    assert saved["name"] == "Ana Ruiz"
    assert {t["topic"]: (t["frequency"], t["target_minutes"]) for t in saved["topics"]} == {
        "music": ("weekly", 20),
        "food": ("daily", 5),
    }


def test_profile_style_options(client: TestClient) -> None:
    options = client.get("/api/options").json()
    assert {"tones", "languages", "voices"} <= options.keys()
    # every tone offered is a real preset: none silently plays as another
    from app.config import tones_cfg
    assert [t["value"] for t in options["tones"]] == list(tones_cfg().presets)

    profile = client.get("/api/profile").json()
    assert profile["voice"] == "aria"
    profile.update(tone="calm", language="es", voice="leo")
    assert client.put("/api/profile", json=profile).status_code == 200
    saved = client.get("/api/profile").json()
    assert (saved["tone"], saved["language"], saved["voice"]) == ("calm", "es", "leo")

    for field in ("tone", "language", "voice"):
        assert client.put("/api/profile", json={**profile, field: "nope"}).status_code == 422


def test_profile_rejects_unknown_or_duplicate_topic(client: TestClient) -> None:
    profile = client.get("/api/profile").json()
    for bad in ("nope", "music"):
        topic = {"topic": bad, "frequency": "daily", "target_minutes": 5}
        profile["topics"] = [topic, topic] if bad == "music" else [topic]
        assert client.put("/api/profile", json=profile).status_code == 422


def test_interests_have_images(client: TestClient) -> None:
    interests = client.get("/api/interests").json()
    assert len(interests) > 10
    assert all(i["image"].startswith("https://") for i in interests)


def test_episodes_newest_first_with_publication_date(client: TestClient, session: Session) -> None:
    client.get("/api/profile")  # creates the implicit user (id 1)
    for title, day in [("old", 1), ("newest", 20), ("middle", 10)]:
        session.add(
            Episode(user_id=1, title=title, published_at=datetime(2026, 11, day, 8, tzinfo=UTC))
        )
    session.commit()

    episodes = client.get("/api/episodes", params={"listened": False}).json()
    assert [e["title"] for e in episodes] == ["newest", "middle", "old"]
    assert episodes[0]["published_at"].startswith("2026-11-20T08:00:00")
    assert episodes[0]["published_at"].endswith(("Z", "+00:00"))


def test_episodes_filter_by_topic_and_listened(client: TestClient, session: Session) -> None:
    client.get("/api/profile")  # creates the implicit user (id 1)
    session.add(Episode(user_id=1, topic="music", title="A", duration_seconds=600))
    session.add(Episode(user_id=1, topic="food", title="B"))
    session.commit()

    new = client.get("/api/episodes", params={"listened": False}).json()
    assert len(new) == 2
    assert client.post("/api/episodes/1/listened").json()["listened"] is True
    assert len(client.get("/api/episodes", params={"listened": False}).json()) == 1

    music = client.get("/api/episodes", params={"topic": "music"}).json()
    assert [e["title"] for e in music] == ["A"]  # includes listened ones
    assert music[0]["topic_label"] == "Music"
    assert music[0]["duration_seconds"] == 600
    assert client.post("/api/episodes/99/listened").status_code == 404


def test_episodes_are_private_to_each_user(client: TestClient, session: Session) -> None:
    ana = client.get("/api/profile").json()  # default user (id 1)
    ben = User(name="Ben")
    session.add(ben)
    session.commit()
    as_ben = {"X-User-Id": str(ben.id)}
    session.add(Episode(user_id=1, title="Ana's"))
    session.add(Episode(user_id=ben.id, title="Ben's"))  # type: ignore[arg-type]
    session.commit()

    assert [e["title"] for e in client.get("/api/episodes").json()] == ["Ana's"]
    assert [e["title"] for e in client.get("/api/episodes", headers=as_ben).json()] == ["Ben's"]
    assert client.get("/api/profile", headers=as_ben).json()["name"] == "Ben"  # gets a profile
    assert ana["name"] != "Ben"

    # Another user's episode looks missing, and listening is recorded per user
    assert client.post("/api/episodes/1/listened", headers=as_ben).status_code == 404
    assert client.post("/api/episodes/1/listened").json()["listened"] is True
    assert session.get(Episode, 1).listened_at is not None  # type: ignore[union-attr]
    assert client.get("/api/episodes", headers={"X-User-Id": "999"}).status_code == 404


def test_settings_topics_are_what_the_pipeline_follows(client: TestClient,
                                                       session: Session) -> None:
    from sqlmodel import select

    from app.database.models import Topic, UserTopic

    profile = client.get("/api/profile").json()
    profile["topics"] = [{"topic": "environment", "frequency": "weekdays", "target_minutes": 8},
                         {"topic": "music", "frequency": "daily", "target_minutes": 4}]
    assert client.put("/api/profile", json=profile).status_code == 200

    rows = {session.get(Topic, ut.topic_id).key: ut  # type: ignore[union-attr]
            for ut in session.exec(select(UserTopic).where(UserTopic.user_id == 1))}
    assert set(rows) == {"climate", "music"}  # "environment" is the climate topic; old ones gone
    assert (rows["climate"].frequency, rows["climate"].target_minutes) == ("weekdays", 8)
    assert [t["topic"] for t in client.get("/api/profile").json()["topics"]] == [
        "environment", "music"]


def test_every_settings_interest_maps_to_a_pipeline_topic(session: Session) -> None:
    """Catalog topics resolve in step 1 by seeded alias: selecting one never costs a model call."""
    from app.config import interests_cfg
    from app.database.models import TopicAlias
    from app.pipeline.step1_topics import normalize, seed_topics
    from app.providers.embeddings import HashingEmbeddings

    seed_topics(session, HashingEmbeddings(64))
    aliases = set(session.exec(select(TopicAlias.alias_normalized)).all())
    assert [i["slug"] for i in interests_cfg() if normalize(i["slug"]) not in aliases] == []
