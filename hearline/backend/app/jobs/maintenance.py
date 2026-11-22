"""Daily clean-up that keeps the fast-growing tables small (queued by the daily plan).

1. fact_cost_daily      totals per day / stage / model, rebuilt from fact_cost_event
   fact_kpi_daily, fact_listener_daily, fact_topic_daily
                        the dashboard's KPI facts for the last 31 days (tracking/kpis.py)
2. fact_cost_event      raw rows older than cost_events_keep_days are deleted (totals stay)
3. fact_episode_story   rows older than heard_rows_keep_days are written to
                        storage/archive/fact_episode_story/day=YYYY-MM-DD.jsonl.gz, then deleted
                        (the 14-day "already heard" check never needs them)
4. ops_job              finished jobs older than done_jobs_keep_days and dead ones older than
                        dead_jobs_keep_days are deleted

Every step is safe to run again: totals are rebuilt, archive files are rewritten whole.
"""

from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import delete
from sqlmodel import Session, col, func, select

from app.config import get_settings, pipeline_cfg
from app.database.models import CostDaily, CostEvent, EpisodeStory, Job, as_utc, utcnow
from app.tracking.kpis import refresh_kpis

log = logging.getLogger(__name__)


@dataclass
class MaintenanceSummary:
    cost_days_summarized: int = 0
    kpi_days_summarized: int = 0
    cost_events_deleted: int = 0
    heard_rows_archived: int = 0
    jobs_deleted: int = 0


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min).replace(tzinfo=utcnow().tzinfo)
    return start, start + timedelta(days=1)


def summarize_costs(session: Session, day: date) -> None:
    """Rebuild the fact_cost_daily rows of one UTC day from the raw cost events."""
    start, end = _day_bounds(day)
    rows = session.exec(
        select(CostEvent.stage, CostEvent.model, func.count(),
               func.sum(CostEvent.input_tokens), func.sum(CostEvent.output_tokens),
               func.sum(CostEvent.cache_read_tokens), func.sum(CostEvent.cache_write_tokens),
               func.sum(CostEvent.searches), func.sum(CostEvent.tts_chars),
               func.sum(CostEvent.cost_usd))
        .where(col(CostEvent.created_at) >= start, col(CostEvent.created_at) < end)
        .group_by(CostEvent.stage, CostEvent.model)).all()
    session.exec(delete(CostDaily).where(col(CostDaily.day) == day))  # type: ignore[call-overload]
    for stage, model, n, i, o, cr, cw, s, chars, cost in rows:
        session.add(CostDaily(day=day, stage=stage, model=model, events=n, input_tokens=i or 0,
                              output_tokens=o or 0, cache_read_tokens=cr or 0,
                              cache_write_tokens=cw or 0, searches=s or 0, tts_chars=chars or 0,
                              cost_usd=round(cost or 0.0, 6)))
    session.commit()


def _days_with(session: Session, column: object, before: datetime) -> list[date]:
    stamps = session.exec(select(column).where(column < before)).all()  # type: ignore[call-overload,operator]
    return sorted({as_utc(s).date() for s in stamps})


def archive_heard_rows(session: Session, before: datetime) -> int:
    """Write fact_episode_story rows older than `before` to one gzip JSON Lines file per day,
    then delete them. A day's file is rewritten whole, so a rerun never duplicates rows."""
    folder = get_settings().archive_dir / "fact_episode_story"
    total = 0
    for day in _days_with(session, EpisodeStory.created_at, before):
        start, end = _day_bounds(day)
        end = min(end, before)
        rows = session.exec(select(EpisodeStory).where(
            col(EpisodeStory.created_at) >= start, col(EpisodeStory.created_at) < end)).all()
        if not rows:
            continue
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"day={day.isoformat()}.jsonl.gz"
        existing = []
        if path.exists():  # a partial day archived before: keep those rows too
            with gzip.open(path, "rt", encoding="utf-8") as f:
                existing = [line for line in f if line.strip()]
        ids = {r.id for r in rows}
        keep = [line for line in existing if json.loads(line)["id"] not in ids]
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.writelines(keep)
            for r in rows:
                f.write(json.dumps({**r.model_dump(), "created_at": as_utc(r.created_at)
                                    .isoformat()}) + "\n")
        session.exec(delete(EpisodeStory).where(col(EpisodeStory.id).in_(ids)))  # type: ignore[call-overload]
        session.commit()
        total += len(rows)
    return total


def run_maintenance(session: Session, now: datetime | None = None) -> MaintenanceSummary:
    now, cfg = now or utcnow(), pipeline_cfg().maintenance
    summary = MaintenanceSummary()

    # 1-2. totals first (yesterday, today so far, and every day about to lose its raw rows)
    cost_cutoff = now - timedelta(days=cfg.cost_events_keep_days)
    days = set(_days_with(session, CostEvent.created_at, cost_cutoff))
    days |= {now.date() - timedelta(days=1), now.date()}
    for day in sorted(days):
        summarize_costs(session, day)
    summary.cost_days_summarized = len(days)
    summary.kpi_days_summarized = refresh_kpis(session, now)
    summary.cost_events_deleted = session.exec(  # type: ignore[call-overload]
        delete(CostEvent).where(col(CostEvent.created_at) < cost_cutoff)).rowcount or 0

    # 3. heard-story rows: never inside the repeat window
    keep_days = max(cfg.heard_rows_keep_days, pipeline_cfg().assemble.seen_window_days + 1)
    summary.heard_rows_archived = archive_heard_rows(session, now - timedelta(days=keep_days))

    # 4. old jobs
    for status, days_kept in (("done", cfg.done_jobs_keep_days),
                              ("dead", cfg.dead_jobs_keep_days)):
        summary.jobs_deleted += session.exec(delete(Job).where(  # type: ignore[call-overload]
            col(Job.status) == status,
            col(Job.finished_at) < now - timedelta(days=days_kept))).rowcount or 0
    session.commit()
    log.info("Maintenance: %s", summary)
    return summary
