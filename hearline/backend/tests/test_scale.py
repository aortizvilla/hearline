"""Scale fixes: bulk daily plan, spread builds, per-user research wait, publish time,
maintenance (cost totals, archive, deletes), TTS throttling retries."""

import gzip
import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, select

from app.config import get_settings, pipeline_cfg
from app.database.models import (
    CostDaily,
    CostEvent,
    Episode,
    EpisodeStory,
    Job,
    Profile,
    User,
    utcnow,
)
from app.jobs import scheduler
from app.jobs.maintenance import run_maintenance
from app.jobs.scheduler import build_time, handle_assemble, plan_daily
from app.jobs.worker import DeferJob, enqueue
from app.providers import Providers
from tests.helpers import add_story, follow, make_user, seeded, topic


def _users(session: Session, n: int) -> list[User]:
    out = []
    for i in range(n):
        user = User(name=f"User {i}")
        session.add(user)
        session.commit()
        session.add(Profile(user_id=user.id, delivery_hour=7,  # type: ignore[arg-type]
                            timezone=["UTC", "Europe/Madrid", "America/New_York"][i % 3]))
        out.append(user)
    session.commit()
    return out


def test_daily_plan_uses_a_handful_of_queries_whatever_the_number_of_users(
        engine, session: Session) -> None:  # type: ignore[no-untyped-def]
    def plan_with(n_new_users: int, day: int) -> tuple[int, int]:
        _users(session, n_new_users)
        count = 0

        def on_execute(*_):  # type: ignore[no-untyped-def]
            nonlocal count
            count += 1
        event.listen(engine, "before_cursor_execute", on_execute)
        summary = plan_daily(session, now=utcnow() + timedelta(days=day))  # one plan per day
        event.remove(engine, "before_cursor_execute", on_execute)
        return count, summary.assemble_jobs

    small, jobs_small = plan_with(5, day=1)
    large, jobs_large = plan_with(120, day=2)
    assert (jobs_small, jobs_large) == (5, 125)
    assert large <= small + 3  # was ~3 statements per user before
    assert plan_daily(session, now=utcnow() + timedelta(days=2)).assemble_jobs == 0  # rerun


def test_builds_are_spread_over_the_window_before_delivery(session: Session) -> None:
    cfg = pipeline_cfg().scheduler
    users = _users(session, 60)
    plan_daily(session, now=utcnow().replace(hour=0, minute=0))
    starts = []
    for job in session.exec(select(Job).where(Job.type == "assemble")):
        deliver_at = scheduler.as_utc(scheduler.datetime.fromisoformat(job.payload["deliver_at"]))
        lead = deliver_at - scheduler.as_utc(job.run_after)
        assert timedelta(minutes=cfg.research_wait_margin_minutes) <= lead <= timedelta(
            hours=cfg.build_lead_hours)
        starts.append(job.run_after)
    assert len(set(starts)) > len(users) * 0.9  # not everyone at the same moment
    when = utcnow()
    assert build_time(7, when) == build_time(7, when)  # same spot for a user every day


def _assemble_job(session: Session, user: User) -> Job:
    today = utcnow().date().isoformat()
    return enqueue(session, "assemble", {"user_id": user.id, "local_date": today,
                                         "research_day": today}, f"assemble:{user.id}")[0]


def test_an_episode_waits_only_for_its_own_topics_and_not_past_the_deadline(
        session: Session, providers: Providers, monkeypatch: pytest.MonkeyPatch) -> None:
    seeded(session, providers)
    ai = topic(session, "ai")
    add_story(session, ai, 1)
    user = make_user(session)
    follow(session, user, ai)
    today = utcnow().date().isoformat()
    deliver_in = {"value": timedelta(hours=1)}
    monkeypatch.setattr(scheduler, "delivery_time", lambda tz, h, d: utcnow() + deliver_in["value"])
    monkeypatch.setattr(scheduler, "build_time", lambda uid, d: d - timedelta(hours=3))
    scheduler.register_handlers()
    job = _assemble_job(session, user)

    enqueue(session, "research", {"topic_key": "space"}, f"research:space:{today}")
    handle_assemble(session, job, providers)  # someone else's topic: no wait
    assert session.exec(select(Episode)).first() is not None

    first = session.exec(select(Episode)).one()  # pretend it failed, stories still unheard
    first.status = "failed"
    for row in session.exec(select(EpisodeStory)):
        session.delete(row)
    session.commit()
    enqueue(session, "research", {"topic_key": "ai"}, f"research:ai:{today}")
    with pytest.raises(DeferJob):  # the user's own topic is still being researched
        handle_assemble(session, job, providers)

    deliver_in["value"] = timedelta(minutes=5)  # too close to delivery: go ahead anyway
    handle_assemble(session, job, providers)
    assert len(session.exec(select(Episode).where(Episode.status == "ready")).all()) == 1


def test_episodes_built_early_stay_hidden_until_delivery(client: TestClient,
                                                        session: Session) -> None:
    client.get("/api/profile")
    session.add(Episode(user_id=1, title="now"))
    session.add(Episode(user_id=1, title="later", published_at=utcnow() + timedelta(hours=2)))
    session.commit()
    assert [e["title"] for e in client.get("/api/episodes").json()] == ["now"]


def test_maintenance_summarizes_archives_and_deletes_old_rows(session: Session) -> None:
    now = utcnow()
    old, recent = now - timedelta(days=100), now - timedelta(hours=1)
    for when, cost in ((old, 1.0), (old, 2.0), (recent, 4.0)):
        session.add(CostEvent(stage="clip_tts", model="tts:polly-neural", tts_chars=10,
                              cost_usd=cost, created_at=when))
    user = make_user(session)
    session.add(Episode(id=1, user_id=user.id))
    session.commit()
    for i, age in enumerate((40, 40, 2)):
        session.add(EpisodeStory(episode_id=1, story_id=i + 1, position=i, user_id=user.id,
                                 created_at=now - timedelta(days=age)))
    for status, age in (("done", 10), ("done", 1), ("dead", 40), ("queued", 50)):
        session.add(Job(type="clip", idempotency_key=f"{status}{age}", status=status,
                        finished_at=now - timedelta(days=age)))
    session.commit()

    first = run_maintenance(session, now)
    assert (first.cost_events_deleted, first.heard_rows_archived, first.jobs_deleted) == (2, 2, 2)
    totals = {d.day: d.cost_usd for d in session.exec(select(CostDaily))}
    assert totals[old.date()] == 3.0 and totals[recent.date()] == 4.0  # totals survive deletes
    assert len(session.exec(select(EpisodeStory)).all()) == 1  # inside the 14-day window: kept
    archive = Path(get_settings().archive_dir) / "fact_episode_story"
    lines = [json.loads(line) for f in archive.glob("*.jsonl.gz")
             for line in gzip.open(f, "rt", encoding="utf-8")]
    assert sorted(r["story_id"] for r in lines) == [1, 2]
    assert {j.idempotency_key for j in session.exec(select(Job))} == {"done1", "queued50"}

    again = run_maintenance(session, now)  # safe to rerun: nothing new, totals unchanged
    assert (again.cost_events_deleted, again.heard_rows_archived, again.jobs_deleted) == (0, 0, 0)
    assert {d.day: d.cost_usd for d in session.exec(select(CostDaily))} == totals


def test_elevenlabs_retries_when_over_its_concurrency_limit(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from app.providers import tts as tts_module
    from app.providers.tts import ElevenLabsTTSProvider

    mp3 = (Path(__file__).parent / "data" / "silence.mp3").read_bytes()
    request = httpx.Request("POST", "https://api.elevenlabs.io")
    replies = iter([httpx.Response(429, request=request), httpx.Response(503, request=request),
                    httpx.Response(200, content=mp3, request=request)])
    calls = []

    def post(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(1)
        return next(replies)
    monkeypatch.setattr(tts_module.httpx, "post", post)
    monkeypatch.setattr(tts_module.time, "sleep", lambda s: None)
    voices = {"aria": {"elevenlabs": "voice123"}}
    result = ElevenLabsTTSProvider("key", "eleven_flash_v2_5", voices).synthesize(
        "Hello there.", tmp_path / "x", "en", "aria")
    assert result.path.exists() and result.duration_sec > 0 and len(calls) == 3
