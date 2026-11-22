"""DB-backed job queue and worker.

- enqueue() is idempotent (unique idempotency_key).
- claim uses SELECT ... FOR UPDATE SKIP LOCKED on Postgres and an atomic conditional
  UPDATE everywhere (so SQLite workers never double-claim).
- failures retry with exponential backoff; after max_attempts (or a permanent error) the
  job moves to the `dead` (dead-letter) state.
- per-type concurrency limits and a global daily USD budget cap (pauses new jobs).
- the long-running worker also runs the daily plan every `scheduler.plan_every_minutes`
  (default hourly). Planning is idempotent, so several workers can do it, and running it
  hourly queues each user's next local delivery as their timezone comes around.

Run with:  python -m app.jobs.worker   (this is the only process the pipeline needs)
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, func, select

from app.config import pipeline_cfg
from app.database.models import Job, utcnow
from app.providers import Providers
from app.providers.llm import LLMError, RefusalError, TransientLLMError
from app.tracking.alerts import alert
from app.tracking.costs import spent_today

log = logging.getLogger(__name__)


class DeferJob(Exception):
    """Raised by a handler to postpone a job without counting an attempt."""

    def __init__(self, seconds: float, reason: str = "") -> None:
        super().__init__(reason)
        self.seconds = seconds


Handler = Callable[[Session, Job, Providers], None]
BatchHandler = Callable[[Session, list[Job], Providers], dict[int, Exception | None]]
HANDLERS: dict[str, Handler] = {}
BATCH_HANDLERS: dict[str, BatchHandler] = {}


# -- queue operations ------------------------------------------------------------------------


def enqueue(session: Session, type_: str, payload: dict[str, Any], key: str,
            run_after: datetime | None = None) -> tuple[Job, bool]:
    """Add a job unless one with the same idempotency key exists. Returns (job, created)."""
    existing = session.exec(select(Job).where(Job.idempotency_key == key)).first()
    if existing:
        return existing, False
    job = Job(type=type_, payload=payload, idempotency_key=key,
              run_after=run_after or utcnow(), max_attempts=pipeline_cfg().scheduler.max_attempts)
    try:
        with session.begin_nested():
            session.add(job)
    except IntegrityError:
        return session.exec(select(Job).where(Job.idempotency_key == key)).one(), False
    session.commit()
    return job, True


def enqueue_many(session: Session, rows: list[tuple[str, dict[str, Any], str, datetime]]
                 ) -> int:
    """Bulk version of enqueue(): (type, payload, key, run_after) rows, inserted in chunks
    with INSERT ... ON CONFLICT DO NOTHING, so existing keys are skipped in the database.
    Returns how many jobs were created."""
    if not rows:
        return 0
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    cfg, now, created = pipeline_cfg().scheduler, utcnow(), 0
    for start in range(0, len(rows), cfg.insert_chunk_size):
        values = [{"type": t, "payload": p, "idempotency_key": k, "run_after": r,
                   "status": "queued", "attempts": 0, "max_attempts": cfg.max_attempts,
                   "created_at": now}
                  for t, p, k, r in rows[start:start + cfg.insert_chunk_size]]
        stmt = insert(Job).values(values).on_conflict_do_nothing(
            index_elements=["idempotency_key"])
        created += session.exec(stmt).rowcount or 0  # type: ignore[call-overload]
    session.commit()
    return created


def _claimable_types(session: Session) -> list[str]:
    limits = pipeline_cfg().scheduler.concurrency
    running = dict(session.exec(
        select(Job.type, func.count()).where(Job.status == "running").group_by(Job.type)
    ).all())
    return [t for t in HANDLERS if running.get(t, 0) < limits.get(t, 1_000_000)]


def claim(session: Session, worker_id: str, now: datetime | None = None,
          type_: str | None = None, limit: int = 1) -> list[Job]:
    """Atomically move up to `limit` due jobs from queued to running."""
    now = now or utcnow()
    types = [type_] if type_ else _claimable_types(session)
    if not types:
        return []
    q = (select(Job).where(Job.status == "queued", col(Job.run_after) <= now,
                           col(Job.type).in_(types))
         .order_by(col(Job.run_after), col(Job.id)).limit(limit))
    if session.get_bind().dialect.name == "postgresql":
        q = q.with_for_update(skip_locked=True)
    claimed = []
    for job in session.exec(q).all():
        res = session.exec(  # type: ignore[call-overload]
            update(Job).where(col(Job.id) == job.id, col(Job.status) == "queued")
            .values(status="running", locked_at=now, locked_by=worker_id,
                    attempts=Job.attempts + 1)
        )
        if res.rowcount == 1:
            claimed.append(job.id)
    session.commit()
    return [session.get(Job, i) for i in claimed]  # type: ignore[misc]


def complete(session: Session, job: Job) -> None:
    job.status, job.finished_at, job.error = "done", utcnow(), None
    session.add(job)
    session.commit()


def fail(session: Session, job: Job, exc: Exception) -> None:
    """Retry with exponential backoff, or dead-letter after max attempts / permanent errors."""
    session.rollback()
    job = session.get(Job, job.id)  # type: ignore[assignment]
    now = utcnow()
    job.locked_at = job.locked_by = None
    if isinstance(exc, DeferJob):
        job.status, job.attempts = "queued", max(0, job.attempts - 1)
        job.run_after = now + timedelta(seconds=exc.seconds)
    else:
        job.error = f"{type(exc).__name__}: {exc}"[:2000]
        permanent = isinstance(exc, RefusalError) or (
            isinstance(exc, LLMError) and not isinstance(exc, TransientLLMError))
        if permanent or job.attempts >= job.max_attempts:
            job.status, job.finished_at = "dead", now
            log.error("Job %s (%s) dead-lettered: %s", job.id, job.type, job.error)
            alert("job_dead", f"Job {job.id} ({job.type}) failed for good after "
                  f"{job.attempts} attempt(s): {job.error}")
        else:
            delay = pipeline_cfg().scheduler.backoff_base_sec * 2 ** (job.attempts - 1)
            job.status, job.run_after = "queued", now + timedelta(seconds=delay)
            log.warning("Job %s (%s) failed (attempt %d), retry in %.0fs: %s",
                        job.id, job.type, job.attempts, delay, job.error)
    session.add(job)
    session.commit()


def requeue_stale(session: Session, now: datetime | None = None) -> int:
    """Put back jobs whose worker died while holding them."""
    now = now or utcnow()
    cutoff = now - timedelta(seconds=pipeline_cfg().scheduler.lock_timeout_sec)
    res = session.exec(  # type: ignore[call-overload]
        update(Job).where(col(Job.status) == "running", col(Job.locked_at) < cutoff)
        .values(status="queued", locked_at=None, locked_by=None)
    )
    session.commit()
    return res.rowcount or 0


def budget_exceeded(session: Session) -> tuple[bool, float, float]:
    cap = pipeline_cfg().scheduler.daily_budget_usd
    spent = spent_today(session)
    return spent >= cap, spent, cap


# -- worker loop -----------------------------------------------------------------------------


@dataclass
class WorkerStats:
    done: int = 0
    failed: int = 0
    deferred: int = 0
    paused_for_budget: bool = False
    plans: int = 0  # daily plans run by this worker
    by_type: dict[str, int] = field(default_factory=dict)


def run_job(session: Session, job: Job, providers: Providers, stats: WorkerStats) -> None:
    try:
        HANDLERS[job.type](session, job, providers)
        complete(session, job)
        stats.done += 1
        stats.by_type[job.type] = stats.by_type.get(job.type, 0) + 1
    except DeferJob as exc:
        fail(session, job, exc)
        stats.deferred += 1
    except Exception as exc:  # noqa: BLE001 - every failure goes through retry/dead-letter
        log.exception("Job %s (%s) raised", job.id, job.type)
        fail(session, job, exc)
        stats.failed += 1


def run_batch(session: Session, jobs: list[Job], providers: Providers,
              stats: WorkerStats) -> None:
    results = BATCH_HANDLERS[jobs[0].type](session, jobs, providers)
    for job in jobs:
        err = results.get(job.id)  # type: ignore[arg-type]
        if err is None:
            complete(session, job)
            stats.done += 1
            stats.by_type[job.type] = stats.by_type.get(job.type, 0) + 1
        else:
            fail(session, job, err)
            stats.failed += 1


def run_worker(engine: Engine, providers: Providers, stop_when_empty: bool = False,
               max_jobs: int | None = None, now_fn: Callable[[], datetime] = utcnow,
               plan: Callable[[Session, datetime], object] | None = None,
               threads: int | None = None) -> WorkerStats:
    """Process jobs forever, or until no due work is left (`stop_when_empty`).

    threads: jobs run in parallel by this process (`scheduler.worker_threads`). Jobs mostly wait
    on the network (LLM, voice, web), so threads multiply throughput; claiming is atomic, so
    threads and worker processes never take the same job. Each thread has its own session.
    plan: called at start and then every `scheduler.plan_every_minutes` (the daily plan), by
    the first thread only."""
    threads = max(1, threads or pipeline_cfg().scheduler.worker_threads)
    base_id = f"{socket.gethostname()}:{os.getpid()}"
    with Session(engine) as session:
        requeue_stale(session)
    if threads == 1:
        return _work(engine, providers, base_id, stop_when_empty, max_jobs, now_fn, plan)
    results: list[WorkerStats] = [WorkerStats() for _ in range(threads)]
    errors: list[BaseException] = []

    def target(n: int) -> None:
        try:
            results[n] = _work(engine, providers, f"{base_id}:{n}", stop_when_empty,
                               max_jobs, now_fn, plan if n == 0 else None)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller below
            errors.append(exc)

    pool = [threading.Thread(target=target, args=(n,), name=f"worker-{n}", daemon=True)
            for n in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    if errors:
        raise errors[0]
    total = WorkerStats()
    for r in results:
        total.done, total.failed, total.deferred = (total.done + r.done,
                                                     total.failed + r.failed,
                                                     total.deferred + r.deferred)
        total.plans += r.plans
        total.paused_for_budget |= r.paused_for_budget
        for k, v in r.by_type.items():
            total.by_type[k] = total.by_type.get(k, 0) + v
    return total


def _work(engine: Engine, providers: Providers, worker_id: str, stop_when_empty: bool,
          max_jobs: int | None, now_fn: Callable[[], datetime],
          plan: Callable[[Session, datetime], object] | None) -> WorkerStats:
    """One worker loop (one thread): plan when due, respect the budget, claim and run jobs."""
    cfg = pipeline_cfg().scheduler
    use_batches = cfg.use_batches and providers.llm.supports_batches
    stats = WorkerStats()
    warned = False
    planned_at: datetime | None = None
    with Session(engine) as session:
        while max_jobs is None or stats.done + stats.failed < max_jobs:
            now = now_fn()
            if plan and (planned_at is None
                         or now - planned_at >= timedelta(minutes=cfg.plan_every_minutes)):
                try:
                    plan(session, now)
                except Exception as exc:  # noqa: BLE001 - a failed plan must not stop the worker
                    session.rollback()
                    log.exception("Daily plan failed; retrying next interval")
                    alert("plan_failed", f"Daily plan failed: {type(exc).__name__}: {exc}")
                planned_at = now
                stats.plans += 1
            over, spent, cap = budget_exceeded(session)
            if over:
                if not warned:
                    log.warning("DAILY BUDGET CAP REACHED: $%.2f spent of $%.2f. New jobs are "
                                "paused until tomorrow (UTC) or until the cap is raised.",
                                spent, cap)
                    alert("budget_cap", f"Daily budget cap reached: ${spent:.2f} of ${cap:.2f}. "
                          "New pipeline jobs are paused until tomorrow (UTC).")
                    warned = True
                stats.paused_for_budget = True
                if stop_when_empty:
                    break
                time.sleep(cfg.poll_interval_sec * 10)
                continue

            jobs = claim(session, worker_id, now)
            if jobs and use_batches and jobs[0].type in BATCH_HANDLERS:
                more = claim(session, worker_id, now_fn(), jobs[0].type,
                             cfg.batch_max_requests - 1)
                run_batch(session, jobs + more, providers, stats)
            elif jobs:
                run_job(session, jobs[0], providers, stats)
            elif stop_when_empty and not _has_due_work(session, now_fn()):
                break
            else:
                time.sleep(cfg.poll_interval_sec if not stop_when_empty else 0.05)
    return stats


def _has_due_work(session: Session, now: datetime) -> bool:
    """True while jobs are running or queued and already due."""
    return session.exec(select(func.count()).select_from(Job).where(
        (col(Job.status) == "running")
        | ((col(Job.status) == "queued") & (col(Job.run_after) <= now)))).one() > 0


def main() -> None:
    from app.database.db import create_db_and_tables, engine
    from app.jobs.scheduler import plan_daily, register_handlers
    from app.providers import build_providers

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    create_db_and_tables()
    register_handlers()
    run_worker(engine, build_providers(), plan=lambda session, now: plan_daily(session, now))


if __name__ == "__main__":
    main()
