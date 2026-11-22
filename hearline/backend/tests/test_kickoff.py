"""New users start right away: first login queues research and a first episode now."""

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.database.models import Job


def jobs(session: Session) -> dict[str, list[dict]]:  # type: ignore[type-arg]
    out: dict[str, list[dict]] = {}  # type: ignore[type-arg]
    for j in session.exec(select(Job)):
        out.setdefault(j.type, []).append({**j.payload, "key": j.idempotency_key})
    return out


def test_first_login_queues_research_and_a_first_episode(client: TestClient,
                                                         session: Session) -> None:
    profile = client.get("/api/profile").json()  # first visit creates the user
    queued = jobs(session)
    topics = {r["topic_key"] for r in queued["research"]}
    assert topics and {r["language"] for r in queued["research"]} == {"en"}
    assert len(queued["assemble"]) == 1 and queued["assemble"][0]["immediate"] is True

    # Spanish right after login: Spanish research is added; still one first episode
    spanish = {**profile, "language": "es", "voice": "aria", "tone": "conversational",
               "topics": [{"topic": "technology", "frequency": "daily", "target_minutes": 5}]}
    assert client.put("/api/profile", json=spanish).status_code == 200
    queued = jobs(session)
    assert {"en", "es"} <= {r["language"] for r in queued["research"]}
    assert len(queued["assemble"]) == 1

    client.put("/api/profile", json=spanish)  # saving again queues nothing new
    keys = [r["key"] for r in jobs(session)["research"]]
    assert len(keys) == len(set(keys))


def test_returning_users_dont_get_an_extra_episode(client: TestClient, session: Session) -> None:
    profile = client.get("/api/profile").json()
    client.put("/api/profile", json=profile)
    client.get("/api/profile")
    assert len(jobs(session)["assemble"]) == 1  # only the first login's episode
