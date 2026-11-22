"""Worker threads run jobs in parallel (no job twice); voice requests are capped; alerts."""

import threading
import time
from pathlib import Path

import pytest
from sqlmodel import Session, select

from app.database.db import make_engine
from app.database.migrate import migrate
from app.database.models import Job
from app.jobs import worker
from app.jobs.worker import enqueue, run_worker
from app.providers import Providers
from app.providers.tts import request_slots
from app.tracking import alerts


@pytest.fixture
def file_engine(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A real SQLite file (WAL), like a worker uses: threads get their own connections."""
    eng = make_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    migrate(eng)
    return eng


def test_threads_run_jobs_in_parallel_and_never_twice(file_engine, providers: Providers,
                                                      monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ran: list[int] = []
    lock = threading.Lock()

    def slow(session, job, providers):  # type: ignore[no-untyped-def]  # e.g. a TTS call
        time.sleep(0.3)
        with lock:
            ran.append(job.id)

    monkeypatch.setattr(worker, "HANDLERS", {"slow": slow})
    monkeypatch.setattr(worker, "BATCH_HANDLERS", {})

    def run(threads: int) -> float:
        with Session(file_engine) as s:
            for i in range(8):
                enqueue(s, "slow", {}, f"slow:{threads}:{i}")
        start = time.monotonic()
        stats = run_worker(file_engine, providers, stop_when_empty=True, threads=threads)
        assert stats.done == 8
        return time.monotonic() - start

    one, four = run(1), run(4)
    assert four < one / 2.5  # ~4x faster for network-bound jobs
    assert len(ran) == len(set(ran)) == 16  # every job exactly once
    with Session(file_engine) as s:
        assert {j.status for j in s.exec(select(Job))} == {"done"}


def test_voice_requests_are_capped_per_process() -> None:
    slots, active, peak = request_slots(2), [0], [0]
    lock = threading.Lock()

    def call() -> None:
        with slots:
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1

    pool = [threading.Thread(target=call) for _ in range(8)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    assert peak[0] == 2  # never above the plan's limit


def test_a_dead_job_raises_one_alert_per_cooldown(session: Session, providers: Providers,
                                                  monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.config import get_settings

    posted: list[dict] = []  # type: ignore[type-arg]
    monkeypatch.setattr(get_settings(), "alert_webhook_url", "https://hooks.example/x")
    monkeypatch.setattr(alerts, "_last_sent", {})
    monkeypatch.setattr(alerts.httpx, "post", lambda url, **kw: posted.append(kw["json"]))
    monkeypatch.setattr(worker, "HANDLERS", {"boom": lambda s, j, p: 1 / 0})
    monkeypatch.setattr(worker, "BATCH_HANDLERS", {})
    monkeypatch.setattr(worker.time, "sleep", lambda _s: None)
    for i in range(3):
        job, _ = enqueue(session, "boom", {}, f"boom:{i}")
        job.max_attempts = 1
        session.add(job)
    session.commit()
    run_worker(session.get_bind(), providers, stop_when_empty=True, threads=1)  # type: ignore[arg-type]
    assert len(posted) == 1  # 3 dead jobs, one message (cooldown)
    assert "job_dead" in posted[0]["text"] and "ZeroDivisionError" in posted[0]["text"]
