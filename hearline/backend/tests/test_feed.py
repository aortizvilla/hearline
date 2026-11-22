"""Private podcast feed: a secret link per listener, valid RSS, redirecting audio links."""

import xml.etree.ElementTree as ET
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.database.models import Episode, User, utcnow

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


def add_episode(session: Session, user_id: int, n: int, **kw) -> Episode:  # type: ignore[no-untyped-def]
    fields = {"status": "ready", "published_at": utcnow() - timedelta(days=n), **kw}
    ep = Episode(user_id=user_id, topic="briefing", title=f"Your Hearline briefing {n}",
                 description=f"Today: story {n}", audio_path=f"users/{user_id}/episodes/{n}.mp3",
                 duration_seconds=200, audio_bytes=1_600_000, **fields)
    session.add(ep)
    session.commit()
    return ep


def test_feed_lists_the_listeners_ready_episodes(client: TestClient, session: Session) -> None:
    links = client.get("/api/podcast").json()
    assert links["feed_url"].startswith("http://localhost:8000/api/feed/")
    assert links["apple_podcasts_url"].startswith("podcast://localhost:8000/api/feed/")
    user = session.exec(select(User)).one()
    a, b = add_episode(session, user.id, 1), add_episode(session, user.id, 2)  # type: ignore[arg-type]
    add_episode(session, user.id, 3, status="failed")  # type: ignore[arg-type]
    add_episode(session, user.id, 4, published_at=utcnow() + timedelta(days=1))  # type: ignore[arg-type]  # not yet
    other = User(name="Other")
    session.add(other)
    session.commit()
    add_episode(session, other.id, 5)  # type: ignore[arg-type]

    path = links["feed_url"].removeprefix("http://localhost:8000")
    resp = client.get(path)
    assert resp.status_code == 200 and resp.headers["content-type"].startswith(
        "application/rss+xml")
    channel = ET.fromstring(resp.content).find("channel")
    assert channel.find(f"{ITUNES}block").text == "Yes"  # type: ignore[union-attr]  # private
    items = channel.findall("item")  # type: ignore[union-attr]
    assert [i.find("title").text for i in items] == [a.title, b.title]  # type: ignore[union-attr]  # newest first; ready only
    enclosure = items[0].find("enclosure")
    assert enclosure.get("length") == "1600000" and enclosure.get("type") == "audio/mpeg"  # type: ignore[union-attr]
    assert items[0].find(f"{ITUNES}duration").text == "200"  # type: ignore[union-attr]

    audio = client.get(enclosure.get("url").removeprefix("http://localhost:8000"),  # type: ignore[union-attr]
                       follow_redirects=False)
    assert audio.status_code == 302 and audio.headers["location"].endswith(
        f"/audio/users/{user.id}/episodes/1.mp3")


def test_the_link_is_the_key_and_can_be_reset(client: TestClient, session: Session) -> None:
    old = client.get("/api/podcast").json()["feed_url"].removeprefix("http://localhost:8000")
    assert client.get(old).status_code == 200
    assert client.get("/api/feed/guessed-token.xml").status_code == 404
    new = client.post("/api/podcast/reset").json()["feed_url"].removeprefix(
        "http://localhost:8000")
    assert new != old and client.get(old).status_code == 404 and client.get(new).status_code == 200
    other = User(name="Other")
    session.add(other)
    session.commit()
    ep = add_episode(session, other.id, 1)  # type: ignore[arg-type]
    token = new.split("/")[-1].removesuffix(".xml")
    # someone else's episode through my link: not found
    assert client.get(f"/api/feed/{token}/episodes/{ep.id}.mp3").status_code == 404
