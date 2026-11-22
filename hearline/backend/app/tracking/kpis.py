"""Daily KPI facts for the admin dashboard, aggregated from the data model one day at a time.

    fact_kpi_daily       day x podcast language x voice x topic ("" = all): additive counts
                         (new users, episodes made / delivered / started / finished, seconds
                         heard, clips) from dim_user + dim_profile, fact_episode, dim_story_clip
    fact_listener_daily  day x user: who listened and how much (unique listeners, retention
                         and stickiness need people, not sums) from fact_episode
    fact_topic_daily     day x topic: followers (a snapshot: follows have no date, so only
                         today's can be taken) and stories researched, from bridge_user_topic
                         and dim_story

summarize_kpis(day) rebuilds one day; it is safe to run again. refresh_kpis() rebuilds the last
kpi_refresh_days (an episode delivered last week can still be listened to today), or every
day since the first user when the tables are empty. The daily maintenance job calls it, and
the dashboard refreshes today before reading.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import case, delete, func
from sqlmodel import Session, col, select

from app.database.models import (
    Episode,
    KpiDaily,
    ListenerDaily,
    Profile,
    Story,
    StoryClip,
    Topic,
    TopicDaily,
    User,
    UserTopic,
    as_utc,
    utcnow,
)

log = logging.getLogger(__name__)
REFRESH_DAYS = 31  # delivered episodes keep being played for weeks; rebuild this many days

# Seconds of an episode heard: what the player reported, else all of it if it was finished.
HEARD = case((col(Episode.progress_seconds) > 0, col(Episode.progress_seconds)),
             (col(Episode.listened).is_(True), func.coalesce(Episode.duration_seconds, 0)),
             else_=0)
PLAYED_AT = func.coalesce(Episode.last_played_at, Episode.listened_at)
STARTED = (col(Episode.progress_seconds) > 0) | col(Episode.listened).is_(True)


def _bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def _rows(session: Session, build: Callable[[bool], Any]) -> Iterator[tuple[str, str, str, list]]:
    """Run a grouped query for all users (topic "") and per followed topic."""
    for lang, voice, *values in session.exec(build(False)):
        yield lang or "", voice or "", "", values
    for lang, voice, topic, *values in session.exec(build(True)):
        yield lang or "", voice or "", topic, values


def _by_follow(q: Any, user_id: Any) -> Any:
    """Add the topic each user follows (a user counts once per topic they follow)."""
    return (q.join(UserTopic, col(UserTopic.user_id) == user_id)
            .join(Topic, col(Topic.id) == UserTopic.topic_id))


def _kpi_rows(session: Session, start: datetime, end: datetime) -> dict[tuple, dict[str, int]]:
    rows: dict[tuple, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    in_day = lambda c: (c >= start) & (c < end)  # noqa: E731

    def new_users(by_topic: bool) -> Any:
        q = select(Profile.language, Profile.voice, *([Topic.key] if by_topic else []),
                   func.count(func.distinct(User.id))).join(User, col(User.id) == Profile.user_id)
        if by_topic:
            q = _by_follow(q, User.id)
        return q.where(in_day(col(User.created_at))).group_by(
            Profile.language, Profile.voice, *([Topic.key] if by_topic else []))

    def made(by_topic: bool) -> Any:
        q = select(Episode.language, Episode.voice, *([Topic.key] if by_topic else []),
                   func.count(), func.count().filter(Episode.status == "ready"),
                   func.count().filter(Episode.status == "failed"))
        if by_topic:
            q = _by_follow(q, Episode.user_id)
        return q.where(in_day(col(Episode.created_at))).group_by(
            Episode.language, Episode.voice, *([Topic.key] if by_topic else []))

    def delivered(by_topic: bool) -> Any:
        q = select(Episode.language, Episode.voice, *([Topic.key] if by_topic else []),
                   func.count(), func.count().filter(STARTED),
                   func.count().filter(col(Episode.listened).is_(True)),
                   func.coalesce(func.sum(HEARD), 0),
                   func.coalesce(func.sum(Episode.duration_seconds).filter(STARTED), 0))
        if by_topic:
            q = _by_follow(q, Episode.user_id)
        return q.where(Episode.status == "ready", in_day(col(Episode.published_at))).group_by(
            Episode.language, Episode.voice, *([Topic.key] if by_topic else []))

    def clips(by_topic: bool) -> Any:
        q = select(StoryClip.language, StoryClip.voice, *([Topic.key] if by_topic else []),
                   func.count())
        if by_topic:
            q = (q.join(Story, col(Story.id) == StoryClip.story_id)
                 .join(Topic, col(Topic.id) == Story.topic_id))
        return q.where(in_day(col(StoryClip.created_at)), col(StoryClip.verified).is_(True)) \
            .group_by(StoryClip.language, StoryClip.voice, *([Topic.key] if by_topic else []))

    for build, fields in (
        (new_users, ["new_users"]),
        (made, ["episodes_created", "episodes_ready", "episodes_failed"]),
        (delivered, ["delivered", "delivered_started", "delivered_finished",
                     "delivered_seconds_heard", "delivered_seconds_started"]),
        (clips, ["clips"]),
    ):
        for lang, voice, topic, values in _rows(session, build):
            for field, value in zip(fields, values, strict=True):
                rows[(lang, voice, topic)][field] += int(value or 0)
    return rows


def summarize_kpis(session: Session, day: date, now: datetime | None = None) -> None:
    """Rebuild one day of the three KPI fact tables (the caller commits)."""
    now = now or utcnow()
    start, end = _bounds(day)
    in_day = lambda c: (c >= start) & (c < end)  # noqa: E731

    session.exec(delete(KpiDaily).where(col(KpiDaily.day) == day))  # type: ignore[call-overload]
    for (lang, voice, topic), measures in _kpi_rows(session, start, end).items():
        if any(measures.values()):
            session.add(KpiDaily(day=day, language=lang, voice=voice, topic=topic, **measures))

    session.exec(delete(ListenerDaily).where(  # type: ignore[call-overload]
        col(ListenerDaily.day) == day))
    for user_id, lang, voice, started, finished, heard in session.exec(
            select(Episode.user_id, Episode.language, Episode.voice,
                   func.count().filter(in_day(col(Episode.started_at))),
                   func.count().filter(in_day(col(Episode.listened_at))),
                   func.coalesce(func.sum(HEARD).filter(in_day(PLAYED_AT)), 0))
            .where(in_day(col(Episode.started_at)) | in_day(col(Episode.listened_at))
                   | in_day(PLAYED_AT))
            .group_by(Episode.user_id, Episode.language, Episode.voice)):
        if started or finished or heard:
            session.add(ListenerDaily(day=day, user_id=user_id, language=lang or "",
                                      voice=voice or "", started=started, finished=finished,
                                      seconds_heard=int(heard)))

    existing = {t.topic_id: t for t in session.exec(
        select(TopicDaily).where(col(TopicDaily.day) == day))}
    stories = dict(session.exec(select(Story.topic_id, func.count()).where(
        in_day(col(Story.first_seen))).group_by(Story.topic_id)).all())
    followers = dict(session.exec(select(UserTopic.topic_id, func.count(
        func.distinct(UserTopic.user_id))).group_by(UserTopic.topic_id)).all()) \
        if day == now.date() else {}
    for topic_id in set(existing) | set(stories) | set(followers):
        row = existing.get(topic_id) or TopicDaily(day=day, topic_id=topic_id)
        row.stories = stories.get(topic_id, 0)
        if day == now.date():  # past days keep the snapshot taken on that day
            row.followers = followers.get(topic_id, 0)
        session.add(row)


def refresh_kpis(session: Session, now: datetime | None = None,
                 days: int = REFRESH_DAYS) -> int:
    """Rebuild the last `days` days, or every day since the first user or episode if
    nothing is stored yet. Returns the number of days rebuilt."""
    now = now or utcnow()
    first = now.date() - timedelta(days=days - 1)
    if session.exec(select(func.count()).select_from(KpiDaily)).one() == 0:
        oldest = [as_utc(d).date() for d in (
            session.exec(select(func.min(User.created_at))).one(),
            session.exec(select(func.min(Episode.created_at))).one()) if d]
        first = min([first, *oldest])
    day = first
    while day <= now.date():
        summarize_kpis(session, day, now)
        day += timedelta(days=1)
    session.commit()
    rebuilt = (now.date() - first).days + 1
    log.info("KPI facts rebuilt for %d day(s) from %s", rebuilt, first)
    return rebuilt
