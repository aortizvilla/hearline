"""Job queue: idempotency, atomic claims, retries with backoff, dead letter, budget cap."""

from datetime import timedelta

import pytest
from sqlmodel import Session

from app.config import pipeline_cfg
from app.database.models import CostEvent, Job, utcnow
from app.jobs import worker
from app.jobs.scheduler import plan_daily, register_handlers
from app.jobs.worker import DeferJob, claim, enqueue, run_worker
from app.providers import Providers
from app.providers.llm import LLMError
from tests.helpers import follow, make_user, seeded, topic


@pytest.fixture
def handlers(monkeypatch):  # type: ignore[no-untyped-def]
    table: dict = {}  # type: ignore[type-arg]
    monkeypatch.setattr(worker, "HANDLERS", table)
    monkeypatch.setattr(worker, "BATCH_HANDLERS", {})
    return table


def test_enqueue_is_idempotent(session: Session) -> None:
    a, created_a = enqueue(session, "research", {"topic_key": "ai"}, "research:ai:2026-11-22")
    b, created_b = enqueue(session, "research", {"topic_key": "ai"}, "research:ai:2026-11-22")
    assert created_a and not created_b and a.id == b.id


def test_claim_is_atomic(session: Session, handlers: dict) -> None:  # type: ignore[type-arg]
    handlers["noop"] = lambda s, j, p: None
    enqueue(session, "noop", {}, "k1")
    first = claim(session, "w1")
    second = claim(session, "w2")
    assert len(first) == 1 and second == []
    assert first[0].status == "running" and first[0].attempts == 1


def test_retry_with_backoff_then_dead_letter(engine, session: Session, providers: Providers,
                                             handlers: dict) -> None:  # type: ignore[no-untyped-def,type-arg]
    calls = []

    def flaky(s, job, p):  # type: ignore[no-untyped-def]
        calls.append(job.attempts)
        raise RuntimeError("boom")

    handlers["flaky"] = flaky
    cfg = pipeline_cfg().scheduler
    cfg.max_attempts, cfg.backoff_base_sec = 3, 10
    job, _ = enqueue(session, "flaky", {}, "flaky-1")
    clock = utcnow()  # virtual "now" for claiming, advanced past each backoff
    for attempt in range(1, 4):
        failed_at = utcnow()
        stats = run_worker(engine, providers, stop_when_empty=True,
                           now_fn=lambda c=clock: c)
        session.refresh(job)
        assert stats.failed == 1
        if attempt < 3:
            backoff = job.run_after.replace(tzinfo=failed_at.tzinfo) - failed_at
            assert job.status == "queued"
            assert backoff >= timedelta(seconds=10 * 2 ** (attempt - 1) - 1)  # 10s, 20s
            clock = job.run_after.replace(tzinfo=failed_at.tzinfo) + timedelta(seconds=1)
    assert job.status == "dead" and "boom" in job.error and calls == [1, 2, 3]


def test_permanent_errors_skip_retries(engine, session: Session, providers: Providers,
                                       handlers: dict) -> None:  # type: ignore[no-untyped-def,type-arg]
    def bad(s, j, p):  # type: ignore[no-untyped-def]
        raise LLMError("400: invalid model")

    handlers["bad"] = bad
    job, _ = enqueue(session, "bad", {}, "bad-1")
    run_worker(engine, providers, stop_when_empty=True)
    session.refresh(job)
    assert job.status == "dead" and job.attempts == 1


def test_defer_does_not_count_as_attempt(engine, session: Session, providers: Providers,
                                         handlers: dict) -> None:  # type: ignore[no-untyped-def,type-arg]
    def wait(s, j, p):  # type: ignore[no-untyped-def]
        raise DeferJob(120, "dependency")

    handlers["wait"] = wait
    job, _ = enqueue(session, "wait", {}, "wait-1")
    stats = run_worker(engine, providers, stop_when_empty=True)
    session.refresh(job)
    assert stats.deferred == 1 and job.status == "queued" and job.attempts == 0


def test_budget_cap_pauses_new_jobs(engine, session: Session, providers: Providers,
                                    handlers: dict, caplog) -> None:  # type: ignore[no-untyped-def,type-arg]
    ran = []
    handlers["work"] = lambda s, j, p: ran.append(j.id)
    pipeline_cfg().scheduler.daily_budget_usd = 1.0
    session.add(CostEvent(stage="research", cost_usd=1.5))
    session.commit()
    enqueue(session, "work", {}, "work-1")
    stats = run_worker(engine, providers, stop_when_empty=True)
    assert stats.paused_for_budget and ran == []
    assert "DAILY BUDGET CAP REACHED" in caplog.text
    pipeline_cfg().scheduler.daily_budget_usd = 10.0
    assert run_worker(engine, providers, stop_when_empty=True).done == 1


def test_daily_plan_end_to_end_is_idempotent(engine, session: Session,
                                             providers: Providers) -> None:  # type: ignore[no-untyped-def]
    register_handlers()
    seeded(session, providers)
    for name in ("Ana", "Ben"):
        follow(session, make_user(session, name), topic(session, "ai"))
    first = plan_daily(session, run_immediately=True)
    assert (first.research_jobs, first.assemble_jobs) == (1, 2)
    stats = run_worker(engine, providers, stop_when_empty=True)
    assert stats.failed == 0 and stats.by_type["assemble"] == 2
    again = plan_daily(session, run_immediately=True)
    assert (again.research_jobs, again.clip_jobs, again.assemble_jobs) == (0, 0, 0)
    assert session.get(Job, 1).status == "done"


def test_assemble_waits_if_the_user_moved_their_delivery_time(session: Session) -> None:
    from app.jobs.scheduler import handle_assemble
    user = make_user(session)
    tomorrow = (utcnow() + timedelta(days=1)).date()
    job, _ = enqueue(session, "assemble", {"user_id": user.id, "local_date": tomorrow.isoformat()},
                     f"assemble:{user.id}:{tomorrow}")
    with pytest.raises(DeferJob):  # tomorrow's delivery hour is still ahead: wait, don't build
        handle_assemble(session, job, None)  # type: ignore[arg-type]


def test_worker_runs_the_daily_plan_on_start_and_every_interval(
        engine, providers: Providers, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(worker.time, "sleep", lambda _s: None)
    clock = [utcnow()]
    planned: list[object] = []

    def tick():  # type: ignore[no-untyped-def]  # each loop turn = 25 minutes
        clock[0] += timedelta(minutes=25)
        return clock[0]

    def plan(session, now):  # type: ignore[no-untyped-def]
        planned.append(now)
        if len(planned) == 3:
            raise KeyboardInterrupt  # stop the endless loop (not swallowed like errors)

    with pytest.raises(KeyboardInterrupt):
        run_worker(engine, providers, now_fn=tick, plan=plan)
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(planned, planned[1:], strict=False)]
    assert gaps == [75.0, 75.0]  # first turn plans; then every >= 60 min (25-minute turns)


def test_a_failing_plan_does_not_stop_the_worker(engine, providers: Providers,
                                                 monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(worker.time, "sleep", lambda _s: None)

    def plan(session, now):  # type: ignore[no-untyped-def]
        raise RuntimeError("db hiccup")

    stats = run_worker(engine, providers, stop_when_empty=True, plan=plan)
    assert stats.plans == 1
