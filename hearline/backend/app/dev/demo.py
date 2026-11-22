"""Demo listeners through the real pipeline, in the one store (dev only).

    python -m app.cli demo                     the listeners below (English and Spanish)
    python -m app.cli demo --email ana@example.com --name Ana --language es --plan free \\
        --topics technology,world              one listener, with transcript and private feed

Works on the store in DATABASE_URL / AUDIO_DIR: migrates it, loads the catalogs the pipeline
needs (topics, subcategories, voices), then plays each listener's first visit through the
real API: dev email sign-in (creates the user if new, with the default profile and their first
episode queued, as in production), their plan, and their Settings, validated against the plan.
Then runs the worker with the real providers from .env (LLM, web search, Wikipedia, TTS) until
the queue is empty, waiting for jobs scheduled a little later.

Nothing is deleted: an existing listener is signed in and updated, research already done today
is reused, and an episode already built today is not rebuilt. Topics overlap on purpose, so
research and recordings are shared between listeners. Afterwards, sign in at
http://localhost:5173 with a listener's email to see and play the podcast.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, func, select

from app.config import get_settings
from app.database.db import get_session, make_engine
from app.database.migrate import migrate
from app.database.models import CostEvent, Episode, Job, User, utcnow
from app.jobs.scheduler import register_handlers
from app.jobs.worker import run_worker
from app.pipeline.step1_topics import seed_subtopics, seed_topics
from app.providers import Providers, build_providers
from app.providers.voices import seed_voices

MAX_WAIT_SEC = 150  # longest pause for a job scheduled later (local research, a deferral)
MINUTES = 5  # per topic: the free plan's maximum, so every plan can use it
LISTENERS: list[dict[str, Any]] = [
    {"name": "Lucía", "email": "lucia@example.com", "language": "es", "plan": "free",
     "voice": "aria", "minutes": MINUTES, "topics": {"technology": [], "world": []}},
    {"name": "Mateo", "email": "mateo@example.com", "language": "es", "plan": "plus",
     "voice": "oliver", "minutes": MINUTES,
     "topics": {"technology": [], "film": ["Cannes Film Festival"]}},
    {"name": "Emma", "email": "emma@example.com", "language": "en", "plan": "free",
     "voice": "leo", "minutes": MINUTES, "topics": {"technology": [], "science": []}},
    {"name": "James", "email": "james@example.com", "language": "en", "plan": "plus",
     "voice": "iris", "minutes": MINUTES, "topics": {"world": [], "science": ["Genetics"]}},
    {"name": "Sofía", "email": "sofia@example.com", "language": "es", "plan": "plus",
     "voice": "nova", "minutes": MINUTES,
     "topics": {"film": [], "history": ["Ancient Egypt"]}},
]


def run_demo(listeners: list[dict[str, Any]] | None = None,
             rebuild: bool = False) -> dict[str, Any]:
    """Add `listeners` (default: LISTENERS), build their episodes, report. With exactly one
    listener the report also has their transcript, sources and private feed. rebuild: delete
    today's episodes of these listeners and the scripts they used, recheck recent background
    terms, and build everything again with the current prompts and rules."""
    listeners = listeners or LISTENERS
    started, since = time.monotonic(), utcnow() - timedelta(seconds=1)
    providers = build_providers()
    engine, audio_dir = open_store(providers)
    api = api_client(engine)
    ids = [add_listener(api, engine, spec) for spec in listeners]
    if rebuild:
        clear_today(engine, audio_dir, providers, ids)
    queue_topic_episodes(engine, ids)
    log: list[str] = []
    drain(engine, providers, log)
    out = report(engine, audio_dir, ids, since, log)
    if len(ids) == 1:
        out.update(episode_details(api, engine, ids[0]))
    return {**out, "minutes": round((time.monotonic() - started) / 60, 1)}


def open_store(providers: Providers) -> tuple[Engine, Path]:
    """The one store (DATABASE_URL / AUDIO_DIR), migrated, with the catalogs loaded."""
    settings = get_settings()
    engine = make_engine(settings.database_url)
    migrate(engine)
    with Session(engine) as s:
        seed_topics(s, providers.embedder)
        seed_subtopics(s, providers.embedder)
        seed_voices(s)
    register_handlers()
    return engine, settings.audio_dir


def api_client(engine: Engine) -> TestClient:
    """The real API, in this process, on the store's database (no startup hooks needed)."""
    from app.main import app

    def session() -> Iterator[Session]:
        with Session(engine) as s:
            yield s
    app.dependency_overrides[get_session] = session
    return TestClient(app)


def add_listener(api: TestClient, engine: Engine, spec: dict[str, Any]) -> int:
    """A listener's first visit (or a returning one): sign in by email, plan, Settings.
    spec: name, email, language, plan, voice, minutes, topics {slug: [subcategories]}."""
    user = api.post("/api/dev/login", json={"email": spec["email"], "name": spec["name"],
                                            "create": True}).json()
    user_id = int(user["user_id"])
    headers = {"X-User-Id": str(user_id)}
    with Session(engine) as s:
        account = s.get(User, user_id)
        if account is not None and account.plan != spec["plan"]:
            from app.plans import start_subscription
            start_subscription(s, account, spec["plan"], source="dev")
    profile = api.get("/api/profile", headers=headers).json()
    settings = {**profile, "name": spec["name"], "email": spec["email"],
                "language": spec["language"], "voice": spec["voice"],
                "tone": spec.get("tone", "conversational"),
                "topics": [{"topic": t, "frequency": "daily", "target_minutes": spec["minutes"],
                            "subtopics": subs} for t, subs in spec["topics"].items()]}
    resp = api.put("/api/profile", json=settings, headers=headers)
    if resp.status_code != 200:
        raise SystemExit(f"{spec['name']}: settings rejected: {resp.status_code} {resp.text}")
    return user_id


def clear_today(engine: Engine, audio_dir: Path, providers: Providers,
                user_ids: list[int]) -> None:
    """Dev only: remove today's episodes of these listeners (rows, story ledger, audio), the
    scripts and recordings of the stories they read, and their assemble jobs; recheck the
    background terms of recent stories on their topics; queue a fresh briefing for each."""
    from app.database.models import EpisodeStory, Story, StoryClip, Topic, UserTopic
    from app.jobs.worker import enqueue_many
    from app.pipeline.step2_research import prune_background

    day_ago = utcnow() - timedelta(hours=20)
    with Session(engine) as s:
        eps = s.exec(select(Episode).where(col(Episode.user_id).in_(user_ids),
                                           col(Episode.created_at) >= day_ago)).all()
        ep_ids = [e.id for e in eps]
        story_ids = set(s.exec(select(EpisodeStory.story_id).where(
            col(EpisodeStory.episode_id).in_(ep_ids))))
        for row in s.exec(select(EpisodeStory).where(col(EpisodeStory.episode_id).in_(ep_ids))):
            s.delete(row)
        s.flush()  # the ledger goes before the episodes it points to
        for e in eps:
            if e.audio_path:
                (audio_dir / e.audio_path).unlink(missing_ok=True)
            s.delete(e)
        for clip in s.exec(select(StoryClip).where(col(StoryClip.story_id).in_(story_ids))):
            if clip.audio_path:
                (audio_dir / clip.audio_path).unlink(missing_ok=True)
            s.delete(clip)
        for job in s.exec(select(Job).where(Job.type == "assemble",
                                            col(Job.created_at) >= day_ago)):
            if int(job.payload.get("user_id", 0)) in user_ids:
                s.delete(job)
        s.commit()
        topic_ids = set(s.exec(select(UserTopic.topic_id).where(
            col(UserTopic.user_id).in_(user_ids))))
        recent = utcnow() - timedelta(hours=96)
        for t in s.exec(select(Topic).where(col(Topic.id).in_(topic_ids))):
            stories = [x for x in s.exec(select(Story).where(
                Story.topic_id == t.id, col(Story.first_seen) >= recent,
                Story.status != "explainer")) if x.background]
            prune_background(s, t, providers, stories)
        now = utcnow()
        rows = []
        for uid in user_ids:
            user = s.get(User, uid)
            tz = user.profile.timezone if user and user.profile else None
            from zoneinfo import ZoneInfo
            local = now.astimezone(ZoneInfo(tz or "UTC")).date().isoformat()
            rows.append(("assemble", {"user_id": uid, "local_date": local,
                                      "research_day": now.date().isoformat(),
                                      "deliver_at": now.isoformat(), "immediate": True},
                         f"assemble:{uid}:{local}", now))
        enqueue_many(s, rows)


def queue_topic_episodes(engine: Engine, user_ids: list[int]) -> None:
    """Listeners whose briefing was already built today (an earlier run) also get today's
    topic episodes; for new listeners the briefing job queues them itself."""
    from app.jobs.scheduler import enqueue_topic_episodes

    with Session(engine) as s:
        for uid in user_ids:
            job = s.exec(select(Job).where(
                Job.type == "assemble", Job.status == "done",
                col(Job.idempotency_key).startswith(f"assemble:{uid}:"),
                col(Job.created_at) >= utcnow() - timedelta(hours=20))
                .order_by(col(Job.id).desc())).first()
            if job is not None and not job.payload.get("topic"):
                enqueue_topic_episodes(s, job, utcnow())


def drain(engine: Engine, providers: Providers, log: list[str]) -> None:
    """Run the worker until no job is queued or running, sleeping until later ones are due."""
    while True:
        stats = run_worker(engine, providers, stop_when_empty=True)
        log.append(f"worker pass: {stats.done} done, {stats.failed} failed, "
                   f"{stats.deferred} deferred {dict(stats.by_type)}")
        with Session(engine) as s:
            nxt = s.exec(select(func.min(Job.run_after)).where(
                col(Job.status).in_(["queued", "running"]))).one()
        if nxt is None:
            return
        wait = (nxt.replace(tzinfo=utcnow().tzinfo) - utcnow()).total_seconds()
        time.sleep(min(MAX_WAIT_SEC, max(1.0, wait + 1)))


def report(engine: Engine, audio_dir: Path, user_ids: list[int], since: Any,
           log: list[str]) -> dict[str, Any]:
    """What these listeners got, and what this run cost."""
    with Session(engine) as s:
        listeners = []
        for uid in user_ids:
            user = s.get(User, uid)
            eps = s.exec(select(Episode).where(
                Episode.user_id == uid, col(Episode.created_at) >= utcnow() - timedelta(hours=20))
                .order_by(col(Episode.id))).all()  # today's briefing and topic episodes
            listeners.append({
                "id": uid, "name": user.name, "email": user.email, "plan": user.plan,  # type: ignore[union-attr]
                "language": user.profile.language if user and user.profile else "",
                "episodes": [{"id": ep.id, "topic": ep.topic, "status": ep.status,
                              "minutes": round((ep.duration_seconds or 0) / 60, 1),
                              "file": str(audio_dir / ep.audio_path) if ep.audio_path else None}
                             for ep in eps]})
        costs: Counter[str] = Counter()
        for e in s.exec(select(CostEvent).where(col(CostEvent.created_at) >= since)):
            costs[e.stage] += e.cost_usd
        dead = [f"{j.type}: {j.error}" for j in s.exec(select(Job).where(
            Job.status == "dead", col(Job.finished_at) >= since))]
        users = s.exec(select(func.count()).select_from(User)).one()
    return {"store": get_settings().database_url, "users_in_store": users,
            "listeners": listeners, "worker": log, "dead_jobs": dead,
            "cost_usd": {k: round(v, 4) for k, v in costs.most_common()},
            "total_usd": round(sum(costs.values()), 4)}


def episode_details(api: TestClient, engine: Engine, uid: int) -> dict[str, Any]:
    """One listener's latest ready episode: transcript, sources, and their private feed as a
    podcast app reads it."""
    feed = api.get("/api/podcast", headers={"X-User-Id": str(uid)}).json()
    xml = api.get(urlsplit(feed["feed_url"]).path)
    items = len(ET.fromstring(xml.content).find("channel").findall("item")) if (  # type: ignore[union-attr]
        xml.status_code == 200) else 0
    with Session(engine) as s:
        ep = s.exec(select(Episode).where(Episode.user_id == uid, Episode.status == "ready",
                                          Episode.topic == "briefing")
                    .order_by(col(Episode.id).desc())).first()
        transcript = sources = ""
        if ep is not None:
            from app.pipeline.step4_assemble import episode_sources, episode_transcript
            transcript = episode_transcript(s, ep)
            sources = "\n".join(f"  - {x['headline']} ({x['source_name']}) {x['url']}"
                                for x in episode_sources(s, ep))
    return {"feed": {"url": feed["feed_url"], "items": items},
            "transcript": transcript, "sources": sources}
