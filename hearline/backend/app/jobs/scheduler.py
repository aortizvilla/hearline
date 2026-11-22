"""Daily plan and job handlers.

plan_daily():
  1. refresh Topic.active_subscribers (users active in the last N days)
  2. enqueue one research job per active topic in English, plus one per local research
     language (research.languages) its active listeners use, 10 minutes later
  3. enqueue clip jobs for new stories x every (tone, language, voice) with a subscriber
     (research jobs also do this for the stories they insert)
  4. enqueue one assemble job per active user, timed inside the window before their local
     delivery time (spread out; the episode becomes visible at delivery). It builds the daily
     briefing, then queues one assemble job per topic due that day (each topic's own episode)
  5. enqueue the daily maintenance job (archive / summarize / delete old rows)

Everything is inserted in bulk (INSERT ... ON CONFLICT DO NOTHING), so planning for 1M users
is a few thousand statements, not millions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlmodel import Session, col, func, select

from app.config import pipeline_cfg, tones_cfg
from app.database.models import (
    Episode,
    Job,
    Profile,
    Story,
    StoryClip,
    Topic,
    User,
    UserTopic,
    as_utc,
    utcnow,
)
from app.jobs.maintenance import run_maintenance
from app.jobs.worker import BATCH_HANDLERS, HANDLERS, DeferJob, enqueue_many
from app.pipeline.step1_topics import refresh_active_subscribers
from app.pipeline.step2_research import research_many, research_topic
from app.pipeline.step3_scripts import ensure_clip, ensure_clips, find_clip
from app.pipeline.step4_assemble import assemble_episode, current_profile, due_subscriptions
from app.providers import Providers

log = logging.getLogger(__name__)
Combo = tuple[str, str, str]  # (tone preset, language, voice)


@dataclass
class PlanSummary:
    active_topics: int = 0
    research_jobs: int = 0
    clip_jobs: int = 0
    assemble_jobs: int = 0
    skipped_inactive_users: int = 0


# -- helpers ---------------------------------------------------------------------------------


LOCAL_RESEARCH_DELAY = timedelta(minutes=10)  # after English: local research skips its events
KICKOFF_LOCAL_DELAY = timedelta(minutes=2)  # a new user's first episode shouldn't wait long


def clip_key(story_id: int, tone: str, language: str, voice: str) -> str:
    return f"clip:{story_id}:{tone}:{language}:{voice}"


def research_key(topic_key: str, language: str, day: str) -> str:
    """Idempotency key of a research job (English keeps the original key format)."""
    return f"research:{topic_key}:{day}" if language == "en" else (
        f"research:{topic_key}:{language}:{day}")


def research_units(session: Session, active_topics: dict[str, int] | list[str],
                   now: datetime) -> list[tuple[str, str]]:
    """(topic key, language) pairs to research: English for every active topic, plus each
    local research language (research.languages) used by the topic's active listeners."""
    local = set(pipeline_cfg().research.languages)
    rows = session.exec(
        select(Topic.key, Profile.language)
        .join(UserTopic, col(UserTopic.topic_id) == Topic.id)
        .join(User, col(User.id) == UserTopic.user_id)
        .join(Profile, col(Profile.user_id) == UserTopic.user_id)
        .where(col(User.last_active_at) >= active_cutoff(now))
    ).all() if local else []
    units = {(key, "en") for key in active_topics}
    units |= {(key, lang) for key, lang in rows if key in active_topics and lang in local}
    return sorted(units)


def active_cutoff(now: datetime) -> datetime:
    return now - timedelta(days=pipeline_cfg().scheduler.inactive_days)


def combos_by_topic(session: Session, now: datetime) -> dict[int, set[Combo]]:
    """Clip combinations actually needed per topic (only active subscribers count)."""
    rows = session.exec(
        select(UserTopic.topic_id, Profile.tone, Profile.language, Profile.voice)
        .join(User, col(User.id) == UserTopic.user_id)
        .join(Profile, col(Profile.user_id) == UserTopic.user_id)
        .where(col(User.last_active_at) >= active_cutoff(now))
    ).all()
    out: dict[int, set[Combo]] = {}
    for topic_id, tone, language, voice in rows:
        out.setdefault(topic_id, set()).add((tones_cfg().preset_for(tone), language, voice))
    return out


def enqueue_clip_jobs(session: Session, stories: list[Story], now: datetime) -> int:
    """Pre-generate clips for each topic's top stories in every combination its active
    subscribers use. Existing clips are found in one query; jobs are inserted in bulk."""
    top_k = pipeline_cfg().scheduler.clip_prefetch_top_k
    combos = combos_by_topic(session, now)
    by_topic: dict[int, list[Story]] = {}
    for s in stories:
        if s.status != "explainer":  # explainers are scripted only if an episode needs them
            by_topic.setdefault(s.topic_id, []).append(s)
    picked = [story for items in by_topic.values()
              for story in sorted(items, key=lambda s: -s.importance_score)[:top_k or None]]
    # a local-language story is only heard by listeners of that language (+ English stories
    # by everyone), so only those combinations are pre-generated
    have = set(session.exec(
        select(StoryClip.story_id, StoryClip.tone, StoryClip.language, StoryClip.voice)
        .where(col(StoryClip.story_id).in_([s.id for s in picked]))).all())
    rows = [("clip", {"story_id": s.id, "tone": tone, "language": language, "voice": voice},
             clip_key(s.id, tone, language, voice), now)
            for s in picked for tone, language, voice in sorted(combos.get(s.topic_id, ()))
            if (s.id, tone, language, voice) not in have
            and (s.language or "en") in ("en", language)]
    return enqueue_many(session, rows)


def delivery_time(timezone: str | None, hour: int | None, local_day: date) -> datetime:
    """UTC time of a delivery hour on a local day."""
    tz = ZoneInfo(timezone or "UTC")
    hour = pipeline_cfg().scheduler.default_delivery_hour if hour is None else hour
    return datetime.combine(local_day, time(hour), tzinfo=tz).astimezone(ZoneInfo("UTC"))


def next_delivery(timezone: str | None, hour: int | None, now: datetime
                  ) -> tuple[datetime, date]:
    """UTC time of the next delivery (today if still ahead) and its local date."""
    local_day = now.astimezone(ZoneInfo(timezone or "UTC")).date()
    when = delivery_time(timezone, hour, local_day)
    if when < now - timedelta(hours=12):  # missed by a lot: deliver tomorrow
        local_day += timedelta(days=1)
        when = delivery_time(timezone, hour, local_day)
    return when.astimezone(now.tzinfo), local_day


def build_time(user_id: int, deliver_at: datetime) -> datetime:
    """When to start building: a fixed spot per user inside the window before delivery, so
    users with the same delivery hour are spread out instead of all at once."""
    cfg = pipeline_cfg().scheduler
    window = max(0.0, cfg.build_lead_hours * 3600 - cfg.research_wait_margin_minutes * 60)
    spread = (user_id * 2654435761 % 2**32) / 2**32  # same user -> same spot, every day
    return deliver_at - timedelta(hours=cfg.build_lead_hours) + timedelta(seconds=window * spread)


# -- daily plan ------------------------------------------------------------------------------


def plan_daily(session: Session, now: datetime | None = None,
               run_immediately: bool = False) -> PlanSummary:
    now = now or utcnow()
    summary = PlanSummary()
    day = now.date().isoformat()

    active = refresh_active_subscribers(session, now)  # 1
    summary.active_topics = len(active)

    summary.research_jobs = enqueue_many(session, [  # 2
        ("research", {"topic_key": key, "language": lang}, research_key(key, lang, day),
         now if lang == "en" else now + LOCAL_RESEARCH_DELAY)
        for key, lang in research_units(session, active, now)])

    lookback = now - timedelta(hours=pipeline_cfg().research.lookback_hours)  # 3
    recent = list(session.exec(select(Story).where(col(Story.first_seen) >= lookback)))
    summary.clip_jobs = enqueue_clip_jobs(session, recent, now)

    # 4: one query for every active user's delivery settings, bulk inserts in chunks
    cutoff = active_cutoff(now)
    summary.skipped_inactive_users = session.exec(select(func.count()).select_from(User).where(
        col(User.last_active_at) < cutoff)).one()
    users = session.exec(
        select(User.id, Profile.timezone, Profile.delivery_hour)
        .outerjoin(Profile, col(Profile.user_id) == User.id)
        .where(col(User.last_active_at) >= cutoff)
        .execution_options(yield_per=pipeline_cfg().scheduler.insert_chunk_size))
    rows: list[tuple[str, dict[str, Any], str, datetime]] = []
    for user_id, timezone, hour in users:
        deliver_at, local_day = next_delivery(timezone, hour, now)
        rows.append(("assemble", {
            "user_id": user_id, "local_date": local_day.isoformat(), "research_day": day,
            "deliver_at": deliver_at.isoformat(), "immediate": run_immediately},
            f"assemble:{user_id}:{local_day}",
            now if run_immediately else max(now, build_time(user_id, deliver_at))))
        if len(rows) >= pipeline_cfg().scheduler.insert_chunk_size:
            summary.assemble_jobs += enqueue_many(session, rows)
            rows = []
    summary.assemble_jobs += enqueue_many(session, rows)

    enqueue_many(session, [("maintenance", {}, f"maintenance:{day}", now)])  # 5
    log.info("Daily plan: %s", summary)
    return summary


def kickoff_user(session: Session, user_id: int, now: datetime | None = None,
                 first_episode: bool = False) -> dict[str, int]:
    """Start the pipeline for one user now instead of waiting for the hourly plan: queue
    today's research for their topics (English + their language if it has local research)
    and, with first_episode (first login), an immediate first episode. The episode job reads
    the user's settings when it runs, so a Settings change right after login is used, and it
    waits until this research is done. Everything is keyed per day: calling it again, or
    the daily plan later, never queues anything twice."""
    now = now or utcnow()
    day = now.date().isoformat()
    profile = current_profile(session, user_id)
    local = profile.language in pipeline_cfg().research.languages
    keys = session.exec(select(Topic.key).join(UserTopic, col(UserTopic.topic_id) == Topic.id)
                        .where(UserTopic.user_id == user_id)).all()
    rows = [("research", {"topic_key": k, "language": "en"}, research_key(k, "en", day), now)
            for k in keys]
    if local:
        rows += [("research", {"topic_key": k, "language": profile.language},
                  research_key(k, profile.language, day), now + KICKOFF_LOCAL_DELAY)
                 for k in keys]
    summary = {"research_jobs": enqueue_many(session, rows), "assemble_jobs": 0}
    if first_episode:
        today = now.astimezone(ZoneInfo(profile.timezone or "UTC")).date()
        summary["assemble_jobs"] = enqueue_many(session, [(
            "assemble", {"user_id": user_id, "local_date": today.isoformat(),
                         "research_day": day, "deliver_at": now.isoformat(),
                         "immediate": True},
            f"assemble:{user_id}:{today}", now)])
    log.info("Kickoff for user %s: %s", user_id, summary)
    return summary


# -- job handlers ----------------------------------------------------------------------------


def handle_research(session: Session, job: Job, providers: Providers) -> None:
    topic = session.exec(select(Topic).where(Topic.key == job.payload["topic_key"])).one()
    result = research_topic(session, topic, providers, force=job.payload.get("force", False),
                            language=job.payload.get("language", "en"))
    stories = [session.get(Story, i) for i in result.new_story_ids]
    enqueue_clip_jobs(session, [s for s in stories if s], utcnow())


def handle_research_batch(session: Session, jobs: list[Job],
                          providers: Providers) -> dict[int, Exception | None]:
    units = {j.id: (session.exec(select(Topic).where(Topic.key == j.payload["topic_key"])).one(),
                    j.payload.get("language", "en")) for j in jobs}
    results = research_many(session, list(units.values()), providers, batch=True)
    out: dict[int, Exception | None] = {}
    for job_id, (topic, lang) in units.items():
        res = results[(topic.key, lang)]
        if isinstance(res, Exception):
            out[job_id] = res  # type: ignore[index]
            continue
        stories = [session.get(Story, i) for i in res.new_story_ids]
        enqueue_clip_jobs(session, [s for s in stories if s], utcnow())
        out[job_id] = None  # type: ignore[index]
    return out


def _clip_spec(session: Session, job: Job) -> tuple[Story, str, str, str]:
    p = job.payload
    story = session.get(Story, p["story_id"])
    if story is None:
        raise ValueError(f"story {p['story_id']} not found")
    return story, p["tone"], p["language"], p["voice"]


def handle_clip(session: Session, job: Job, providers: Providers) -> None:
    ensure_clip(session, *_clip_spec(session, job), providers)


def handle_clip_batch(session: Session, jobs: list[Job],
                      providers: Providers) -> dict[int, Exception | None]:
    specs = [_clip_spec(session, j) for j in jobs]
    ensure_clips(session, specs, providers, batch=True)
    out: dict[int, Exception | None] = {}
    for job, (story, tone, language, voice) in zip(jobs, specs, strict=True):
        found = find_clip(session, story.id, tones_cfg().preset_for(tone), language, voice)  # type: ignore[arg-type]
        out[job.id] = None if found else RuntimeError("clip not generated")  # type: ignore[index]
    return out


def _pending_research(session: Session, user_id: int, research_day: str) -> int:
    """How many of this user's topics still have today's research queued or running (the
    English research, and the research in the user's own language if it is a local one)."""
    language = session.exec(select(Profile.language).where(Profile.user_id == user_id)).first()
    langs = ["en", *([language] if language in pipeline_cfg().research.languages else [])]
    keys = [research_key(k, lang, research_day) for k in session.exec(
        select(Topic.key).join(UserTopic, col(UserTopic.topic_id) == Topic.id)
        .where(UserTopic.user_id == user_id)) for lang in langs]
    return session.exec(select(func.count()).select_from(Job).where(
        col(Job.idempotency_key).in_(keys), col(Job.status).in_(["queued", "running"]))).one()


def enqueue_topic_episodes(session: Session, job: Job, now: datetime) -> int:
    """After a user's briefing: one assemble job per topic due that day, for each topic's own
    episode (same delivery). Not with a single topic: the briefing is already that episode."""
    p = job.payload
    day = date.fromisoformat(p["local_date"])
    slugs = sorted(due_subscriptions(session, p["user_id"], day).values(),
                   key=lambda ut: ut.id or 0)
    if len(slugs) < 2:
        return 0
    return enqueue_many(session, [
        ("assemble", {**p, "topic": ut.interest},
         f"assemble:{p['user_id']}:{p['local_date']}:{ut.interest}", now) for ut in slugs])


def handle_assemble(session: Session, job: Job, providers: Providers) -> None:
    p, now = job.payload, utcnow()
    topic = p.get("topic")  # a topic episode; None = the daily briefing
    if topic is None:
        try:
            build_episode(session, job, providers, None)
        except DeferJob:
            raise  # still waiting for research: the topic episodes are queued after it
        except Exception:
            enqueue_topic_episodes(session, job, now)  # they don't need the briefing
            raise
        enqueue_topic_episodes(session, job, now)
    else:
        build_episode(session, job, providers, topic)


def build_episode(session: Session, job: Job, providers: Providers, topic: str | None) -> None:
    p, now = job.payload, utcnow()
    user_id, local_day = p["user_id"], date.fromisoformat(p["local_date"])
    research_day = p.get("research_day", now.date().isoformat())
    if p.get("immediate"):  # run-daily --immediate: build now, wait up to the lead time
        deliver_at = None
        deadline = as_utc(job.created_at) + timedelta(
            hours=pipeline_cfg().scheduler.build_lead_hours)
    else:
        # The run time came from the profile at planning time. Re-read the latest profile:
        # if the user moved their delivery later since then, wait for the new build time.
        profile = current_profile(session, user_id)
        deliver_at = delivery_time(profile.timezone, profile.delivery_hour, local_day)
        wait = (build_time(user_id, deliver_at) - now).total_seconds()
        if wait > 60:
            raise DeferJob(wait, "delivery time changed in the profile")
        deadline = deliver_at - timedelta(
            minutes=pipeline_cfg().scheduler.research_wait_margin_minutes)

    pending = _pending_research(session, user_id, research_day)
    if pending and now < deadline:  # wait only for this user's own topics
        raise DeferJob(60, f"{pending} of the user's topics still researching")
    if pending:
        log.warning("User %s: building without %d unfinished topic(s) to deliver on time",
                    user_id, pending)

    since = (deliver_at or now) - timedelta(hours=20)
    already = session.exec(select(Episode).where(
        Episode.user_id == user_id, Episode.status == "ready", Episode.topic == (
            topic or "briefing"), col(Episode.published_at) >= since)).first()
    if already:  # idempotent: a retried job must not produce a second episode
        log.info("User %s already has this delivery's episode %s", user_id, already.id)
        return
    episode = assemble_episode(session, user_id, providers, publish_at=deliver_at, topic=topic)
    if isinstance(episode, Episode) and episode.status == "ready":
        record_timing(session, episode, user_id, research_day)


def _longest(session: Session, keys: list[str]) -> float:
    """Seconds of the longest finished job among these keys (they run in parallel)."""
    if not keys:
        return 0.0
    rows = session.exec(select(Job.locked_at, Job.finished_at).where(
        col(Job.idempotency_key).in_(keys), Job.status == "done")).all()
    return max([(as_utc(f) - as_utc(s)).total_seconds() for s, f in rows if s and f] or [0.0])


def record_timing(session: Session, episode: Episode, user_id: int, research_day: str) -> None:
    """End-to-end generation time of a ready episode: the critical path of the jobs that built
    it, today's research of its topics (in parallel: the longest), then the clip jobs of its
    stories (in parallel: the longest), then its own assembly. Waits in the queue and the
    planned wait before delivery are not counted: this is how long it takes to make."""
    from app.database.models import EpisodeStory

    language = episode.language
    langs = ["en", *([language] if language in pipeline_cfg().research.languages else [])]
    topic_keys = session.exec(
        select(Topic.key).join(Story, col(Story.topic_id) == Topic.id)
        .join(EpisodeStory, col(EpisodeStory.story_id) == Story.id)
        .where(EpisodeStory.episode_id == episode.id).distinct()).all()
    research = _longest(session, [research_key(k, lang, research_day)
                                  for k in topic_keys for lang in langs])
    story_ids = session.exec(select(EpisodeStory.story_id).where(
        EpisodeStory.episode_id == episode.id)).all()
    clips = _longest(session, [
        clip_key(sid, episode.tone, language, episode.voice) for sid in story_ids])
    assembly = float((episode.timing or {}).get("assembly", 0.0))
    episode.timing = {"research": round(research, 1), "clips": round(clips, 1),
                      "assembly": round(assembly, 1)}
    episode.generation_seconds = round(research + clips + assembly, 1)
    session.add(episode)
    session.commit()


def handle_maintenance(session: Session, job: Job, providers: Providers) -> None:
    run_maintenance(session)


def register_handlers() -> None:
    HANDLERS.update(research=handle_research, clip=handle_clip, assemble=handle_assemble,
                    maintenance=handle_maintenance)
    BATCH_HANDLERS.update(research=handle_research_batch, clip=handle_clip_batch)
