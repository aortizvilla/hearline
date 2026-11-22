"""The admin dashboard, read from the daily fact tables (see tracking/kpis.py for how they are
aggregated): fact_kpi_daily, fact_listener_daily, fact_topic_daily and fact_cost_daily.
Pipeline health is the only live part: it describes "now".

Every KPI is computed for the chosen window (the last `days` days) and for the window just
before it, so the UI can show the change (month over month with the default 30 days).

- listeners: distinct users who heard something; retention: last window's listeners who
  listened again in this one; stickiness: listening days per listener
- listen rate / start rate: delivered episodes finished / played at all
- completion rate: share of each started episode actually heard ("consumption")
- hours listened, cost per listener, cost per hour listened
- cost per episode: all cost / episodes made ready, split into shared (research and scripts,
  shared by every listener who gets them) and extra (what one more episode adds: its own
  phrases and its accuracy review)
- time to generate an episode, end to end (median and p95): the critical path of the jobs
  that built it (research, scripts and recordings, assembly), stored per episode

Filters (all optional): podcast `language`, `voice` and `topic` key. A topic means the users
who follow it. Cost is shared across users, so it is filtered by date only (`cost_filtered`);
topic followers are counted for every language and voice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.config import pipeline_cfg, ui_options
from app.database.models import (
    CostDaily,
    CostEvent,
    Episode,
    Job,
    KpiDaily,
    ListenerDaily,
    Profile,
    Topic,
    TopicDaily,
    User,
    UserTopic,
    Voice,
    as_utc,
    utcnow,
)
from app.jobs.maintenance import summarize_costs
from app.tracking.costs import spent_today
from app.tracking.kpis import refresh_kpis

OVERDUE = timedelta(minutes=10)  # a queued job this late means the worker is behind
WORKER_IDLE = timedelta(minutes=30)  # no finished job for this long, with work overdue: down
SUMS = ["new_users", "episodes_created", "episodes_ready", "episodes_failed", "delivered",
        "delivered_started", "delivered_finished", "delivered_seconds_heard",
        "delivered_seconds_started", "clips"]


@dataclass
class Filters:
    days: int = 30
    language: str | None = None
    voice: str | None = None
    topic: str | None = None  # Topic.key


@dataclass
class Window:
    """Days [first, end)."""

    first: date
    end: date

    def before(self) -> Window:
        return Window(self.first - (self.end - self.first), self.first)


def _ratio(a: float, b: float) -> float | None:
    return a / b if b else None


def _in(day_column: Any, w: Window) -> Any:
    return (day_column >= w.first) & (day_column < w.end)


def _kpis(f: Filters, w: Window, *columns: Any) -> Any:
    q = select(*columns).where(_in(col(KpiDaily.day), w), KpiDaily.topic == (f.topic or ""))
    if f.language:
        q = q.where(KpiDaily.language == f.language)
    if f.voice:
        q = q.where(KpiDaily.voice == f.voice)
    return q


def _listening(f: Filters, w: Window) -> Any:
    """fact_listener_daily rows matching the filters (a subquery)."""
    q = select(ListenerDaily).where(_in(col(ListenerDaily.day), w))
    if f.language:
        q = q.where(ListenerDaily.language == f.language)
    if f.voice:
        q = q.where(ListenerDaily.voice == f.voice)
    if f.topic:
        q = q.where(col(ListenerDaily.user_id).in_(select(UserTopic.user_id).join(
            Topic, col(Topic.id) == UserTopic.topic_id).where(Topic.key == f.topic)))
    return q.subquery()


def _cost(session: Session, w: Window) -> tuple[dict[str, float], dict[str, float]]:
    """(USD by day, USD by stage) from fact_cost_daily."""
    by_day: dict[str, float] = {}
    by_stage: dict[str, float] = {}
    for d, stage, usd in session.exec(select(CostDaily.day, CostDaily.stage, CostDaily.cost_usd)
                                      .where(_in(col(CostDaily.day), w))):
        by_day[d.isoformat()] = by_day.get(d.isoformat(), 0.0) + usd
        by_stage[stage] = by_stage.get(stage, 0.0) + usd
    return by_day, by_stage


PER_EPISODE_STAGES = {"phrase_write", "phrase_tts", "episode_review"}  # what one more costs


def _dt(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=UTC)


def _ready_episodes(session: Session, w: Window) -> int:
    """Episodes made ready in the window, every language, voice and topic (cost is shared)."""
    return session.exec(select(func.count()).select_from(Episode).where(
        Episode.status == "ready", col(Episode.created_at) >= _dt(w.first),
        col(Episode.created_at) < _dt(w.end))).one()


def _generation(session: Session, f: Filters, w: Window) -> list[Episode]:
    q = select(Episode).where(Episode.status == "ready",
                              col(Episode.generation_seconds).is_not(None),
                              col(Episode.created_at) >= _dt(w.first),
                              col(Episode.created_at) < _dt(w.end))
    if f.language:
        q = q.where(Episode.language == f.language)
    if f.voice:
        q = q.where(Episode.voice == f.voice)
    if f.topic:
        q = q.where(col(Episode.user_id).in_(select(UserTopic.user_id).join(
            Topic, col(Topic.id) == UserTopic.topic_id).where(Topic.key == f.topic)))
    return list(session.exec(q))


def _percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (p in 0-100), None without values."""
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, round(p / 100 * len(ordered)) - 1))], 1)


def generation_kpis(session: Session, f: Filters, w: Window) -> dict[str, Any]:
    eps = _generation(session, f, w)
    total = [e.generation_seconds for e in eps if e.generation_seconds is not None]
    part = {k: _percentile([float((e.timing or {}).get(k, 0.0)) for e in eps], 50)
            for k in ("research", "clips", "assembly")}
    return {"generation_median": _percentile(total, 50),  # type: ignore[arg-type]
            "generation_p95": _percentile(total, 95),  # type: ignore[arg-type]
            "generation_parts": part}


def _user_count(session: Session, f: Filters, before: date) -> int:
    """Users signed up before `before` (dim_user), by their profile and followed topic."""
    q = select(func.count(func.distinct(User.id))).where(
        col(User.created_at) < datetime.combine(before, time.min, tzinfo=UTC))
    if f.language or f.voice:
        q = q.join(Profile, col(Profile.user_id) == User.id)
        if f.language:
            q = q.where(Profile.language == f.language)
        if f.voice:
            q = q.where(Profile.voice == f.voice)
    if f.topic:
        q = q.where(col(User.id).in_(select(UserTopic.user_id).join(
            Topic, col(Topic.id) == UserTopic.topic_id).where(Topic.key == f.topic)))
    return session.exec(q).one()


def period_kpis(session: Session, f: Filters, w: Window) -> dict[str, Any]:
    """Every headline number for one window."""
    k = dict(zip(SUMS, session.exec(_kpis(f, w, *[
        func.coalesce(func.sum(getattr(KpiDaily, m)), 0) for m in SUMS])).one(), strict=True))

    lst = _listening(f, w)
    listeners, seconds = session.exec(select(func.count(func.distinct(lst.c.user_id)),
                                             func.coalesce(func.sum(lst.c.seconds_heard), 0))).one()
    listening_days = session.exec(select(func.count()).select_from(
        select(lst.c.user_id, lst.c.day).distinct().subquery())).one()
    prev = _listening(f, w.before())
    before = session.exec(select(func.count(func.distinct(prev.c.user_id)))).one()
    stayed = session.exec(select(func.count(func.distinct(lst.c.user_id))).where(
        lst.c.user_id.in_(select(prev.c.user_id)))).one()

    stories = select(func.coalesce(func.sum(TopicDaily.stories), 0)).where(
        _in(col(TopicDaily.day), w))
    if f.topic:
        stories = stories.join(Topic, col(Topic.id) == TopicDaily.topic_id).where(
            Topic.key == f.topic)

    _, by_stage = _cost(session, w)
    cost = sum(by_stage.values())
    hours = seconds / 3600
    ready = _ready_episodes(session, w)
    extra = sum(v for k, v in by_stage.items() if k in PER_EPISODE_STAGES)
    return {
        "users": _user_count(session, f, w.end),
        "new_users": k["new_users"],
        "listeners": listeners,
        "retention": _ratio(stayed, before),
        "stickiness": _ratio(listening_days, listeners),
        "listen_rate": _ratio(k["delivered_finished"], k["delivered"]),
        "start_rate": _ratio(k["delivered_started"], k["delivered"]),
        "completion_rate": _ratio(k["delivered_seconds_heard"], k["delivered_seconds_started"]),
        "hours": round(hours, 2),
        "minutes_per_listener": _ratio(hours * 60, listeners),
        "episodes": k["episodes_created"],
        "ready_rate": _ratio(k["episodes_ready"], k["episodes_created"]),
        "failed": k["episodes_failed"],
        "funnel": {"delivered": k["delivered"], "started": k["delivered_started"],
                   "finished": k["delivered_finished"]},
        "cost": round(cost, 4),
        "cost_by_stage": by_stage,
        "cost_per_listener": _ratio(cost, listeners),
        "cost_per_hour": _ratio(cost, hours),
        "cost_per_episode": _ratio(cost, ready),
        "shared_cost_per_episode": _ratio(cost - extra, ready),
        "extra_cost_per_episode": _ratio(extra, ready),
        **generation_kpis(session, f, w),
        "stories": session.exec(stories).one(),
        "clips": k["clips"],
    }


def daily_series(session: Session, f: Filters, w: Window) -> dict[str, list[Any]]:
    """Day-by-day values of a window as lists aligned with `days` (day i of the current window
    lines up with day i of the previous one, for the dashed comparison line)."""
    days = [w.first + timedelta(days=i) for i in range((w.end - w.first).days)]
    out: dict[str, list[Any]] = {"days": [d.isoformat() for d in days]}
    sums = {d: row for d, *row in session.exec(_kpis(f, w, KpiDaily.day, *[
        func.coalesce(func.sum(getattr(KpiDaily, m)), 0) for m in SUMS]).group_by(KpiDaily.day))}
    for i, m in enumerate(SUMS):
        out[m] = [int(sums[d][i]) if d in sums else 0 for d in days]
    lst = _listening(f, w)
    listening = {d: (n, s) for d, n, s in session.exec(
        select(lst.c.day, func.count(func.distinct(lst.c.user_id)),
               func.coalesce(func.sum(lst.c.seconds_heard), 0)).group_by(lst.c.day))}
    out["listeners"] = [listening.get(d, (0, 0))[0] for d in days]
    out["hours"] = [round(listening.get(d, (0, 0))[1] / 3600, 3) for d in days]
    cost = _cost(session, w)[0]
    out["cost"] = [round(cost.get(d.isoformat(), 0.0), 4) for d in days]
    ready = {d: n for d, n in session.exec(
        select(func.date(Episode.created_at), func.count()).where(
            Episode.status == "ready", col(Episode.created_at) >= _dt(w.first),
            col(Episode.created_at) < _dt(w.end)).group_by(func.date(Episode.created_at)))}
    out["cost_per_episode"] = [
        round(cost.get(d.isoformat(), 0.0) / ready[d.isoformat()], 4)
        if ready.get(d.isoformat()) else None for d in days]
    gen: dict[str, list[float]] = {}
    for e in _generation(session, f, w):
        gen.setdefault(as_utc(e.created_at).date().isoformat(), []).append(
            e.generation_seconds or 0.0)
    out["generation_median"] = [_percentile(gen.get(d.isoformat(), []), 50) for d in days]
    users, total = [], _user_count(session, f, w.first)  # running total of sign-ups
    for n in out["new_users"]:
        total += n
        users.append(total)
    out["users"] = users
    return out


def retention_weeks(session: Session, f: Filters, end: date,
                    weeks: int = 8) -> list[dict[str, Any]]:
    """Week-over-week retention for the last `weeks` weeks ending at `end` (exclusive):
    of the listeners of the week before, how many listened again this week."""
    out = []
    for i in range(weeks, 0, -1):
        week = Window(end - timedelta(days=7 * i), end - timedelta(days=7 * (i - 1)))
        now, before = _listening(f, week), _listening(f, week.before())
        prev_n = session.exec(select(func.count(func.distinct(before.c.user_id)))).one()
        listeners = session.exec(select(func.count(func.distinct(now.c.user_id)))).one()
        kept = session.exec(select(func.count(func.distinct(now.c.user_id))).where(
            now.c.user_id.in_(select(before.c.user_id)))).one()
        out.append({"week": week.first.isoformat(), "listeners": listeners,
                    "returning": kept, "new": listeners - kept, "rate": _ratio(kept, prev_n)})
    return out


def _followers(session: Session, w: Window) -> dict[str, tuple[str, int]]:
    """Topic key -> (label, followers) from the latest snapshot inside the window."""
    latest = (select(TopicDaily.topic_id, func.max(TopicDaily.day).label("day"))
              .where(_in(col(TopicDaily.day), w), col(TopicDaily.followers).is_not(None))
              .group_by(TopicDaily.topic_id).subquery())
    rows = session.exec(
        select(Topic.key, Topic.display_name, TopicDaily.followers)
        .join(TopicDaily, col(TopicDaily.topic_id) == Topic.id)
        .join(latest, (latest.c.topic_id == TopicDaily.topic_id)
              & (latest.c.day == TopicDaily.day)))
    return {key: (label, n or 0) for key, label, n in rows}


def top_topics(session: Session, w: Window, limit: int = 10) -> list[dict[str, Any]]:
    now, before = _followers(session, w), _followers(session, w.before())
    ranked = sorted(now.items(), key=lambda kv: -kv[1][1])[:limit]
    return [{"key": key, "label": label, "followers": n,
             "previous": before[key][1] if key in before else None}
            for key, (label, n) in ranked if n]


def health(session: Session, now: datetime) -> dict[str, Any]:
    jobs = dict(session.exec(select(Job.status, func.count()).group_by(Job.status)).all())
    overdue = session.exec(select(func.count()).select_from(Job).where(
        Job.status == "queued", col(Job.run_after) < now - OVERDUE)).one()
    dead_24h = session.exec(select(func.count()).select_from(Job).where(
        Job.status == "dead", col(Job.finished_at) >= now - timedelta(days=1))).one()
    failed_24h = session.exec(select(func.count()).select_from(Episode).where(
        Episode.status == "failed", col(Episode.created_at) >= now - timedelta(days=1))).one()
    last_done = session.exec(select(func.max(Job.finished_at))).one()
    last_done = as_utc(last_done) if last_done else None
    spent = spent_today(session, now)
    budget = pipeline_cfg().scheduler.daily_budget_usd
    worker_down = overdue > 0 and (last_done is None or now - last_done > WORKER_IDLE)
    status = ("critical" if worker_down or spent >= budget
              else "warning" if overdue or dead_24h or failed_24h or spent >= 0.8 * budget
              else "good")
    return {"status": status,
            "jobs": {s: jobs.get(s, 0) for s in ("queued", "running", "done", "dead")},
            "overdue": overdue, "dead_24h": dead_24h, "failed_24h": failed_24h,
            "last_job_at": last_done.isoformat() if last_done else None,
            "spent_today": round(spent, 4), "daily_budget": budget}


def filter_options(session: Session) -> dict[str, list[dict[str, str]]]:
    topics = session.exec(
        select(Topic.key, Topic.display_name).join(UserTopic, col(UserTopic.topic_id) == Topic.id)
        .distinct().order_by(Topic.display_name)).all()
    voices = session.exec(select(Voice.key, Voice.label).where(col(Voice.active).is_(True))
                          .order_by(Voice.sort_order)).all()
    return {"languages": ui_options()["languages"],
            "voices": [{"value": k, "label": label} for k, label in voices],
            "topics": [{"value": k, "label": label} for k, label in topics]}


def refresh_today(session: Session, now: datetime) -> None:
    """Bring today's facts up to date before reading (older days: the maintenance job).
    Days with cost events but no daily total yet (a fresh database) are summarized too."""
    summarized = {d.isoformat() for d in session.exec(select(CostDaily.day).distinct())}
    raw = {str(d)[:10] for d in session.exec(
        select(func.date(CostEvent.created_at)).distinct())}
    for d in sorted(raw - summarized) + [now.date().isoformat()]:
        summarize_costs(session, date.fromisoformat(d))
    refresh_kpis(session, now, days=1)  # also backfills everything when nothing is stored


def dashboard(session: Session, f: Filters, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    refresh_today(session, now)
    w = Window(now.date() - timedelta(days=f.days - 1), now.date() + timedelta(days=1))
    prev = w.before()
    return {
        "range": {"from": w.first.isoformat(), "to": now.date().isoformat(),
                  "previous_from": prev.first.isoformat(),
                  "previous_to": (prev.end - timedelta(days=1)).isoformat(),
                  "days": [(w.first + timedelta(days=i)).isoformat() for i in range(f.days)]},
        "current": period_kpis(session, f, w),
        "previous": period_kpis(session, f, prev),
        "series": daily_series(session, f, w),
        "previous_series": daily_series(session, f, prev),
        "retention_weeks": retention_weeks(session, f, w.end),
        "topics": top_topics(session, w),
        "cost_filtered": not (f.language or f.voice or f.topic),
        "topics_filtered": not (f.language or f.voice),
        "health": health(session, now),
        "options": filter_options(session),
    }
