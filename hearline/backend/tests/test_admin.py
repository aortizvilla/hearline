"""Admin dashboard: who may open it, and that its KPIs, deltas and filters add up."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.config import get_settings
from app.database.models import (
    CostDaily,
    CostEvent,
    Episode,
    EpisodeStory,
    Job,
    KpiDaily,
    ListenerDaily,
    TopicDaily,
    User,
    utcnow,
)
from app.tracking.dashboard import Filters, Window, dashboard, period_kpis
from tests.helpers import add_story, follow, make_user, topic
from tests.test_auth import clerk, token  # noqa: F401  (fixture)


def test_dev_mode_everyone_is_admin(client: TestClient) -> None:
    assert client.get("/api/admin/access").json() == {"admin": True}
    body = client.get("/api/admin/dashboard?days=7").json()
    assert {"current", "previous", "series", "topics", "health", "options"} <= body.keys()
    assert len(body["range"]["days"]) == 7


@pytest.mark.usefixtures("clerk")
def test_clerk_only_listed_users_are_admin(client: TestClient,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    other = {"Authorization": f"Bearer {token('user_other')}"}
    assert client.get("/api/admin/access", headers=other).json() == {"admin": False}
    assert client.get("/api/admin/dashboard", headers=other).status_code == 403

    monkeypatch.setattr(get_settings(), "admin_user_ids", ["user_boss"])
    boss = {"Authorization": f"Bearer {token('user_boss')}"}
    assert client.get("/api/admin/access", headers=boss).json() == {"admin": True}
    assert client.get("/api/admin/dashboard", headers=boss).status_code == 200


def test_player_progress_only_moves_forward(client: TestClient, session: Session) -> None:
    client.get("/api/profile")  # creates the dev user
    user = session.exec(select(User)).first()
    assert user is not None
    ep = Episode(user_id=user.id, status="ready", duration_seconds=600,
                 published_at=utcnow() - timedelta(hours=1))
    session.add(ep)
    session.commit()

    def report(seconds: int) -> int:
        r = client.post(f"/api/episodes/{ep.id}/progress", json={"seconds": seconds})
        return r.json()["progress_seconds"]

    assert report(300) == 300
    assert report(100) == 300  # seeking back keeps the furthest point
    assert report(9999) == 600  # capped at the episode length
    session.refresh(ep)
    assert ep.started_at is not None and ep.last_played_at is not None


def test_kpis_deltas_and_filters(client: TestClient, session: Session) -> None:  # seeds topics
    now = utcnow()
    month_ago = now - timedelta(days=40)
    ana = make_user(session, "Ana", language="es", voice="leo")
    bo = make_user(session, "Bo", language="en", voice="aria")
    tech, science = topic(session, "technology"), topic(session, "science")
    follow(session, ana, tech)
    follow(session, bo, tech)
    follow(session, bo, science)
    story = add_story(session, tech, 1)
    # This window: Ana hears half of her episode, then marks it listened; Bo's episode failed.
    ep_ana = Episode(user_id=ana.id, status="ready", language="es", voice="leo",
                     duration_seconds=600, progress_seconds=300, listened=True, listened_at=now,
                     last_played_at=now, published_at=now - timedelta(hours=2))
    ep_bo = Episode(user_id=bo.id, status="failed", language="en", voice="aria")
    # Previous window: both listened, so Ana is retained and Bo churned.
    old = [Episode(user_id=u.id, status="ready", language=lang, voice=v, duration_seconds=600,
                   listened=True, listened_at=month_ago, published_at=month_ago,
                   created_at=month_ago)
           for u, lang, v in ((ana, "es", "leo"), (bo, "en", "aria"))]
    session.add_all([ep_ana, ep_bo, *old])
    session.commit()
    session.add(EpisodeStory(episode_id=ep_ana.id, story_id=story.id, position=0, user_id=ana.id))
    session.add(CostEvent(stage="research", cost_usd=1.5))
    session.add(CostEvent(stage="research", cost_usd=3.0, created_at=month_ago))
    old_day = (now - timedelta(days=100)).date()
    session.add(CostDaily(day=old_day, stage="research", model="m", cost_usd=9.0))
    session.add(Job(type="clip", idempotency_key="late", run_after=now - timedelta(hours=1)))
    session.commit()

    body = dashboard(session, Filters(days=30), now)
    cur, prev = body["current"], body["previous"]
    assert (cur["users"], cur["listeners"], prev["listeners"]) == (2, 1, 2)
    assert cur["retention"] == 0.5  # 1 of last month's 2 listeners came back
    assert (cur["listen_rate"], cur["start_rate"], cur["completion_rate"]) == (1.0, 1.0, 0.5)
    assert cur["funnel"] == {"delivered": 1, "started": 1, "finished": 1}
    assert cur["hours"] == round(300 / 3600, 2) and prev["hours"] == round(1200 / 3600, 2)
    assert (cur["episodes"], cur["failed"], cur["ready_rate"]) == (2, 1, 0.5)
    assert (cur["cost"], prev["cost"], cur["cost_per_listener"]) == (1.5, 3.0, 1.5)
    assert body["topics"][0] == {"key": "technology", "label": tech.display_name, "followers": 2,
                                 "previous": None}  # no snapshot was taken last month
    assert body["series"]["days"][-1] == now.date().isoformat()  # today is the last point
    assert body["series"]["listeners"][-1] == 1 and sum(body["series"]["listeners"]) == 1
    assert body["series"]["users"][-1] == 2  # running total of sign-ups
    assert len(body["previous_series"]["days"]) == 30
    last = body["retention_weeks"][-1]
    assert (last["listeners"], last["returning"], last["new"]) == (1, 0, 1)
    assert body["cost_filtered"]
    assert body["health"]["status"] == "critical"  # overdue job and no job ever finished

    spanish = dashboard(session, Filters(days=30, language="es"), now)
    assert spanish["current"]["users"] == 1 and spanish["current"]["episodes"] == 1
    assert spanish["previous"]["listeners"] == 1 and spanish["current"]["retention"] == 1.0
    assert not spanish["cost_filtered"]

    science_only = dashboard(session, Filters(days=30, topic="science"), now)  # = its followers
    assert science_only["current"]["users"] == 1  # only Bo follows science
    assert science_only["current"]["episodes"] == 1  # Bo's (failed) episode
    assert science_only["current"]["listeners"] == 0 and science_only["previous"]["listeners"] == 1

    # older days have no raw events left: their daily total fills in
    assert dashboard(session, Filters(days=120), now)["current"]["cost"] == 13.5


def test_dashboard_reads_the_stored_daily_facts(client: TestClient, session: Session) -> None:
    """The facts are kept in the data model: once a day is aggregated, the dashboard no longer
    needs the raw rows (only today is refreshed when it opens)."""
    now = utcnow()
    month_ago = now - timedelta(days=40)
    ana = make_user(session, "Ana")
    follow(session, ana, topic(session, "technology"))
    session.add(Episode(user_id=ana.id, status="ready", language="en", voice="aria",
                        duration_seconds=600, listened=True, listened_at=month_ago,
                        published_at=month_ago, created_at=month_ago))
    session.commit()

    dashboard(session, Filters(days=30), now)  # first use: backfills every day
    assert session.exec(select(KpiDaily).where(KpiDaily.day == month_ago.date())).all()
    assert session.exec(select(ListenerDaily)).one().seconds_heard == 600
    assert session.exec(select(TopicDaily).where(TopicDaily.day == now.date())).one().followers == 1

    for ep in session.exec(select(Episode)).all():  # the raw rows go away...
        session.delete(ep)
    session.commit()
    prev = Window(now.date() - timedelta(days=59), now.date() - timedelta(days=29))
    kpis = period_kpis(session, Filters(), prev)  # ...the stored facts still answer
    assert (kpis["listeners"], kpis["episodes"], kpis["listen_rate"]) == (1, 1, 1.0)
