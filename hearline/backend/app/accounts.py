"""Deleting a listener and everything that is theirs (GDPR erasure), from the app or a Clerk
webhook. Shared content (stories, clips, phrases) is not personal and stays: it belongs to no one
and other listeners use it. Cost rows keep no personal data (a numeric reference at most).
"""

from __future__ import annotations

import logging

from sqlmodel import Session, col, delete, select

from app.database.models import (
    Episode,
    EpisodeStory,
    Job,
    ListenerDaily,
    Profile,
    Subscription,
    User,
    UserTopic,
)
from app.pipeline.step1_topics import refresh_active_subscribers
from app.providers.storage import get_storage

log = logging.getLogger(__name__)


def delete_account(session: Session, user: User) -> dict[str, int]:
    """Delete the user, their settings, subscriptions, episodes (rows and audio), listening
    history, daily listener stats and queued jobs. Returns how many rows of each went."""
    uid = user.id
    storage, gone = get_storage(), {}
    episodes = session.exec(select(Episode).where(Episode.user_id == uid)).all()
    for ep in episodes:
        if ep.audio_path:
            try:
                storage.delete(ep.audio_path)
            except Exception as exc:  # a file already gone must not block the erasure
                log.warning("Could not delete %s: %s", ep.audio_path, exc)
    open_jobs = session.exec(select(Job).where(col(Job.status).in_(("queued", "running"))))
    pending = [j.id for j in open_jobs if (j.payload or {}).get("user_id") == uid]
    for table in (EpisodeStory, ListenerDaily, UserTopic, Subscription, Profile):
        gone[table.__tablename__] = session.exec(delete(table).where(table.user_id == uid)).rowcount  # type: ignore[attr-defined]
    gone["fact_episode"] = session.exec(delete(Episode).where(Episode.user_id == uid)).rowcount  # type: ignore[attr-defined]
    if pending:
        gone["ops_job"] = session.exec(delete(Job).where(col(Job.id).in_(pending))).rowcount  # type: ignore[attr-defined]
    session.delete(user)
    session.commit()
    refresh_active_subscribers(session)   # research stops for topics only they followed
    log.info("Deleted account %s: %s", uid, gone)
    return gone
