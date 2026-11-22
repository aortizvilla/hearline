"""Step 4: per-user episode assembly from shared stories and cached scripts. No LLM call here.

1. load profile + the topics due today
2. unseen candidate stories (14-day ledger)
3. rank and fit to target length, scripts from step 3
4. greeting / transitions / outro from step 5 (step5_phrases.py)
5. stitch the audio with step 6 (step6_audio.py), export MP3
   accuracy review of the whole episode (review.py): flagged segments out, re-plan
6. persist Episode, story ledger, transcript, sources

dry_run stops after 4: it builds the transcript without recording audio or saving an episode.

Two kinds of episode, each with its own "already heard" ledger: the daily briefing (all the
topics due today, topic="briefing") and one per topic (topic=<interest slug>: that topic
alone, at the minutes chosen for it). They share the same scripts and recordings, so a topic
episode mostly costs the stitching.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlmodel import Session, col, select

from app.config import (
    get_settings,
    interest_images,
    interest_labels,
    pipeline_cfg,
    plans_cfg,
    tones_cfg,
)
from app.database.models import (
    Episode,
    EpisodeStory,
    PhraseClip,
    Profile,
    Story,
    StoryClip,
    Topic,
    User,
    UserTopic,
    as_utc,
    utcnow,
)
from app.pipeline.step1_topics import normalize
from app.pipeline.step2_research import canonicalize_url as canonical
from app.pipeline.step3_scripts import ensure_clips, find_clip, speech_seconds
from app.pipeline.step5_phrases import ensure_phrases, episode_phrases, rundown_phrases
from app.pipeline.step6_audio import sound, stitch
from app.providers import Providers
from app.providers.storage import get_storage

log = logging.getLogger(__name__)

@dataclass
class Candidate:
    story: Story
    topic: Topic
    score: float
    est_seconds: float


@dataclass
class EpisodePlan:
    user_id: int
    tone: str
    language: str
    voice: str
    topics: list[Topic] = field(default_factory=list)
    items: list[tuple[Candidate, StoryClip]] = field(default_factory=list)
    frame: dict[str, Any] = field(default_factory=dict)
    transcript: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
    est_seconds: float = 0.0
    delivery_hour: int = 7  # picks the greeting (morning / afternoon / evening)
    skip_teasers: set[int] = field(default_factory=set)  # teasers the review rejected


# -- 1-2. candidates ----------------------------------------------------------------------


def current_profile(session: Session, user_id: int) -> Profile:
    """The user's profile as it is in the database right now.

    Profiles are edited in place (one row per user), so the stored row is always the latest.
    populate_existing makes sure a copy cached earlier in a long-lived session (the worker)
    is overwritten with what's in the database, never reused.
    """
    profile = session.exec(select(Profile).where(Profile.user_id == user_id)
                           .execution_options(populate_existing=True)).first()
    return profile or Profile(user_id=user_id)


def matches_keywords(story: Story, keywords: list[str]) -> bool:
    if not keywords:
        return True
    text = normalize(f"{story.headline} {story.summary}")
    return any(normalize(k) in text for k in keywords)


def is_due(frequency: str, day: date) -> bool:
    """Does a topic with this frequency play on this (local) day?"""
    if frequency == "weekdays":
        return day.weekday() < 5
    if frequency == "weekly":
        return day.weekday() == pipeline_cfg().assemble.weekly_weekday
    return True


def lookback_hours(frequency: str) -> int:
    """How far back a topic's unheard stories count. Daily topics reach back to
    `backfill_hours` too: recency decay keeps today's news first, so older stories only fill
    time that today's news leaves empty (a slow news day)."""
    cfg = pipeline_cfg().assemble
    if frequency == "weekly":
        return cfg.weekly_lookback_hours
    return max(cfg.lookback_hours, cfg.backfill_hours)


def due_subscriptions(session: Session, user_id: int, day: date,
                      only: str | None = None) -> dict[int, UserTopic]:
    """The user's topics that play today, by topic id (only: one interest slug)."""
    return {ut.topic_id: ut for ut in session.exec(
        select(UserTopic).where(UserTopic.user_id == user_id))
        if is_due(ut.frequency, day) and only in (None, ut.interest)}


def target_minutes(subs: dict[int, UserTopic], profile: Profile,
                   plan_cap: int | None = None) -> int:
    """Episode length: the sum of today's topic minutes, else the profile's length; capped by
    the pipeline and by the listener's plan (config/plans.yaml)."""
    minutes = [ut.target_minutes for ut in subs.values() if ut.target_minutes]
    total = sum(minutes) if minutes else profile.target_minutes
    return min(total, pipeline_cfg().assemble.max_episode_minutes, plan_cap or total)


def local_day(profile: Profile, when: datetime) -> date:
    return when.astimezone(ZoneInfo(profile.timezone or "UTC")).date()


def load_candidates(session: Session, user: User, now: datetime,
                    profile: Profile | None = None, day: date | None = None,
                    only: str | None = None) -> list[Candidate]:
    """Recent stories on the user's topics due today (only: one interest slug, for a topic
    episode) that the user hasn't heard recently in this kind of episode."""
    cfg = pipeline_cfg().assemble
    profile = profile or current_profile(session, user.id)  # type: ignore[arg-type]
    subs = due_subscriptions(session, user.id, day or local_day(profile, now), only)  # type: ignore[arg-type]
    if not subs:
        return []
    topics = {t.id: t for t in session.exec(select(Topic).where(col(Topic.id).in_(list(subs))))}
    oldest = now - timedelta(hours=max(lookback_hours(ut.frequency) for ut in subs.values()))
    stories = [s for s in session.exec(select(Story).where(
        col(Story.topic_id).in_(list(subs)),
        col(Story.published_at) >= oldest,
        Story.status != "retracted",
        col(Story.language).in_(["en", profile.language]),  # international + own language
    )) if as_utc(s.published_at) >= now - timedelta(
        hours=lookback_hours(subs[s.topic_id].frequency))]
    seen = set(session.exec(select(EpisodeStory.story_id)
                            .join(Episode, col(Episode.id) == EpisodeStory.episode_id).where(
        EpisodeStory.user_id == user.id,
        col(EpisodeStory.created_at) >= now - timedelta(days=cfg.seen_window_days),
        Episode.topic == (only or "briefing"),  # each kind of episode keeps its own ledger
    )))
    superseded = {s.update_of_id for s in stories if s.update_of_id and s.id not in seen}
    tone = tones_cfg().preset_for(profile.tone)
    durations = {c.story_id: c.duration_sec for c in session.exec(select(StoryClip).where(
        col(StoryClip.story_id).in_([s.id for s in stories]), StoryClip.tone == tone,
        StoryClip.language == profile.language, StoryClip.voice == profile.voice))}

    out: list[Candidate] = []
    for s in stories:
        ut = subs[s.topic_id]
        explainer = s.status == "explainer"
        if (s.id in seen or s.id in superseded
                or (s.importance_score < cfg.min_importance and not explainer)
                or not matches_keywords(s, ut.keyword_filters)):
            continue
        if explainer:  # background context: ranks after all news, only fills leftover time
            out.append(Candidate(s, topics[s.topic_id], cfg.explainer_score * ut.weight,
                                 durations.get(s.id) or cfg.default_clip_seconds))
            continue
        age_h = max(0.0, (now - as_utc(s.published_at)).total_seconds() / 3600)
        recency = 0.5 ** (age_h / cfg.recency_half_life_hours)
        focus = 1 + cfg.subtopic_boost if set(s.subtopics or []) & set(ut.subtopics or []) else 1
        if s.language != "en" and s.language == profile.language:
            focus *= 1 + cfg.local_language_boost  # local news in the listener's language
        focus *= source_weight(s.canonical_url)  # trusted outlets up, demoted ones down
        out.append(Candidate(s, topics[s.topic_id],
                             s.importance_score * ut.weight * recency * focus,
                             durations.get(s.id) or cfg.default_clip_seconds))
    return drop_same_events(out)


def source_weight(url: str) -> float:
    """Ranking weight of a story's outlet (research.source_weights, by domain; 1.0 if unlisted).
    A domain also covers its subdomains (bbc.co.uk covers news.bbc.co.uk)."""
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    for domain, weight in pipeline_cfg().research.source_weights.items():
        if host == domain or host.endswith("." + domain):
            return weight
    return 1.0


NAME = re.compile(r"(?<![.!?:¿¡«\"]\s)(?<!^)\b([A-ZÁÉÍÓÚÑÄÖÜÀÈÌÒÙÇ][\w'’-]{2,}|\d{3,})")


def story_names(story: Story) -> set[str]:
    """Names in a story's headline and summary (capitalised words that don't start a
    sentence, and numbers like 007), minus its outlets: what the story is about."""
    outlets = {w.lower() for name in [story.source_name, *(o.get("name", "") for o in
                                                          story.sources or [])]
               for w in re.findall(r"\w+", name or "")}
    found = set()
    for text in (story.headline, story.summary):
        for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
            found |= {m.lower() for m in NAME.findall(sentence)}
    return found - outlets


def _similarity(a: Story, b: Story) -> float:
    va, vb = a.embedding or [], b.embedding or []
    return sum(x * y for x, y in zip(va, vb, strict=True)) if va and len(va) == len(vb) else 0.0


def same_subject(a: Story, b: Story) -> bool:
    """Two different headlines about one subject (e.g. "Pattinson reappears as the new James
    Bond" and "James Bond, a licence to make money" after the casting news)."""
    cfg = pipeline_cfg().assemble
    na, nb = story_names(a), story_names(b)
    shared = na & nb
    return (len(shared) >= cfg.same_subject_min_names
            and len(shared) >= cfg.same_subject_name_share * min(len(na), len(nb))
            and _similarity(a, b) >= cfg.same_subject_min_similarity)


def drop_same_events(candidates: list[Candidate]) -> list[Candidate]:
    """One event, one story per episode: a candidate whose headline means the same as a
    better-ranked one (any topic: e.g. the same news under Technology and Business, from two
    different outlets), or that tells the same subject again under another headline, is
    dropped. Uses the stored headline embeddings, so it's free."""
    threshold = pipeline_cfg().research.headline_dup_threshold
    kept: list[Candidate] = []
    for c in sorted(candidates, key=lambda c: -c.score):
        if c.story.status != "explainer" and any(
                k.story.status != "explainer" and (
                    _similarity(c.story, k.story) >= threshold or same_subject(c.story, k.story))
                for k in kept):
            continue
        kept.append(c)
    return kept


# -- 3. ranking and length fitting -----------------------------------------------------------


def select_stories(candidates: list[Candidate], target_seconds: float,
                   topic_seconds: dict[int, float] | None = None) -> list[Candidate]:
    """Fill the target length: best story of each topic first, then the best of the rest.

    topic_seconds: each topic's own budget (the minutes the user chose for it); a topic stops
    taking stories when it is full, so one topic can't crowd out the others. `max_per_topic`
    is a safety cap. When there aren't enough good stories the episode is shorter, never
    padded. Always returns at least one story when any exist. Output is grouped by topic
    (topics ordered by their best story) and ranked within each topic.
    """
    cfg = pipeline_cfg().assemble
    budget = target_seconds - cfg.frame_overhead_seconds
    ranked = sorted(candidates, key=lambda c: -c.score)
    chosen: list[Candidate] = []
    per_topic: dict[int, int] = defaultdict(int)
    topic_used: dict[int, float] = defaultdict(float)
    used = 0.0

    explainers = 0

    def try_add(c: Candidate) -> None:
        nonlocal used, explainers
        tid: int = c.topic.id  # type: ignore[assignment]
        if c.story.status == "explainer" and (explainers >= cfg.max_explainers
                                              or explained_story(c, chosen) is None):
            return  # background only ever follows the story it explains
        cost = c.est_seconds + (cfg.transition_seconds if per_topic[tid] == 0 else 0)
        topic_full = (topic_seconds is not None and per_topic[tid] > 0
                      and topic_used[tid] + cost > topic_seconds.get(tid, float("inf")))
        if per_topic[tid] >= cfg.max_per_topic or topic_full or (chosen and used + cost > budget):
            return
        chosen.append(c)
        explainers += c.story.status == "explainer"
        per_topic[tid] += 1
        topic_used[tid] += cost
        used += cost

    best_per_topic: dict[int, Candidate] = {}
    for c in ranked:
        best_per_topic.setdefault(c.topic.id, c)  # type: ignore[arg-type]
    for c in best_per_topic.values():  # pass 1: coverage (at least one per topic)
        try_add(c)
    for c in ranked:  # pass 2: fill by score
        if c not in chosen:
            try_add(c)

    topic_order = {tid: i for i, tid in enumerate(best_per_topic)}
    news = sorted((c for c in chosen if c.story.status != "explainer"),
                  key=lambda c: (topic_order[c.topic.id], -c.score))  # type: ignore[index]
    out: list[Candidate] = []
    for c in news:  # each background segment right after the story it explains
        out.append(c)
        out += [e for e in chosen if e.story.status == "explainer"
                and explained_story(e, news) is c]
    return out


def explained_story(explainer: Candidate, chosen: list[Candidate]) -> Candidate | None:
    """The chosen news story whose background this explainer is (same topic), if any."""
    url = explainer.story.canonical_url
    for c in chosen:
        if c.story.status != "explainer" and c.topic.id == explainer.topic.id and any(
                canonical(b.get("url", "")) == url for b in c.story.background or []):
            return c
    return None


# -- 4. intro / transitions / outro --------------------------------------------------------


def frame_for(session: Session, providers: Providers, plan: EpisodePlan, first_name: str,
              day: date, ref: str, synthesize: bool
              ) -> tuple[list[PhraseClip], list[PhraseClip], PhraseClip]:
    """Greeting, rundown, transitions and outro from the shared phrase cache (step 5: written
    and recorded only the first time anyone needs them). Fills plan.frame; returns
    (opening = greeting + rundown line + teasers, one transition per topic, outro)."""
    greeting, transitions, outro = episode_phrases(first_name, plan.delivery_hour,
                                                   plan.topics, day, plan.language)
    best_first = [c.story for c, _ in sorted(plan.items, key=lambda item: -item[0].score)
                  if c.story.status != "explainer"  # the rundown previews news, not context
                  and c.story.id not in plan.skip_teasers]
    rundown = rundown_phrases(best_first)
    phrases = ensure_phrases(session, providers, [greeting, *rundown, *transitions, outro],
                             plan.tone, plan.language, plan.voice, ref, synthesize)
    plan.frame = {
        "intro": phrases[greeting.key].text,
        "rundown": [phrases[s.key].text for s in rundown],
        "transitions": [{"topic_key": t.key, "line": phrases[s.key].text}
                        for t, s in zip(plan.topics, transitions, strict=True)],
        "outro": phrases[outro.key].text,
        "teaser_ids": [s.id for s in best_first[:len(rundown) - 1]] if rundown else [],
    }
    opening = [phrases[greeting.key], *(phrases[s.key] for s in rundown)]
    return opening, [phrases[s.key] for s in transitions], phrases[outro.key]


Item = tuple[Topic, Story, StoryClip]  # one story read in an episode, in order


def render_transcript(frame: dict[str, Any], items: list[Item]) -> str:
    """Spoken text with a source line under every story."""
    transitions = {t["topic_key"]: t["line"] for t in frame.get("transitions", [])}
    out = [f"[Intro] {frame.get('intro', '')}"]
    if frame.get("rundown"):
        out.append("[Coming up] " + " ".join(frame["rundown"]))
    current: int | None = None
    for topic, story, clip in items:
        if topic.id != current:
            out.append(f"\n[{topic.display_name}] {transitions.get(topic.key, '')}")
            current = topic.id
        out.append(f"{clip.script}\n  (Source: {story.source_name}, {story.canonical_url})")
    out.append(f"\n[Outro] {frame.get('outro', '')}")
    return "\n".join(out)


def build_transcript(plan: EpisodePlan) -> str:
    items = [(t, cand.story, clip) for t in plan.topics
             for cand, clip in plan.items if cand.topic.id == t.id]
    return render_transcript(plan.frame, items)


def episode_items(session: Session, episode: Episode) -> list[Item]:
    """The stories an episode read, in order, with the exact clip used."""
    rows = session.exec(
        select(Topic, Story, StoryClip)
        .join(EpisodeStory, col(EpisodeStory.story_id) == Story.id)
        .join(Topic, col(Topic.id) == Story.topic_id)
        .join(StoryClip, (col(StoryClip.story_id) == Story.id)
              & (col(StoryClip.tone) == episode.tone)
              & (col(StoryClip.language) == episode.language)
              & (col(StoryClip.voice) == episode.voice))
        .where(EpisodeStory.episode_id == episode.id)
        .order_by(col(EpisodeStory.position))
    ).all()
    return [(t, s, c) for t, s, c in rows]


def episode_transcript(session: Session, episode: Episode) -> str:
    """Rebuilt from the stored frame + fact_episode_story + the clip scripts."""
    return render_transcript(episode.frame, episode_items(session, episode))


def episode_sources(session: Session, episode: Episode) -> list[dict[str, str]]:
    return [{"headline": s.headline, "source_name": s.source_name, "url": s.canonical_url,
             "topic": t.key} for t, s, _ in episode_items(session, episode)]


# -- main entry point -----------------------------------------------------------------------


def plan_episode(session: Session, user: User, providers: Providers, now: datetime,
                 synthesize: bool, day: date | None = None,
                 only: str | None = None, exclude: set[int] | None = None
                 ) -> EpisodePlan | None:
    """Steps 1-4: choose stories, make sure their clips exist, write the frame.
    `day` is the user's local delivery day (picks which topics play); `only` an interest
    slug for a topic episode (that topic alone, at its own minutes)."""
    cfg = pipeline_cfg().assemble
    profile = current_profile(session, user.id)  # type: ignore[arg-type]  # read once per episode
    day = day or local_day(profile, now)
    plan = EpisodePlan(user.id, tones_cfg().preset_for(profile.tone), profile.language,  # type: ignore[arg-type]
                       profile.voice, delivery_hour=profile.delivery_hour)
    candidates = load_candidates(session, user, now, profile, day, only)
    subs = due_subscriptions(session, user.id, day, only)  # type: ignore[arg-type]
    tier = plans_cfg().get(user.plan)  # Settings are kept within it; this is a safety net
    topic_seconds = {tid: min(ut.target_minutes or profile.target_minutes,
                              tier.max_topic_minutes) * 60.0
                     for tid, ut in subs.items()}
    length_seconds = (sum(topic_seconds.values()) + cfg.frame_overhead_seconds if only
                      else target_minutes(subs, profile, tier.max_episode_minutes) * 60)
    if not candidates:
        return None

    excluded: set[int] = set(exclude or ())  # e.g. segments the accuracy review removed
    chosen: list[Candidate] = []
    clips: dict[int, StoryClip] = {}
    for _ in range(cfg.max_refill_rounds):
        pool = [c for c in candidates if c.story.id not in excluded]
        previous, chosen = chosen, select_stories(pool, length_seconds, topic_seconds)
        if not chosen:
            break
        specs = [(c.story, plan.tone, plan.language, plan.voice) for c in chosen]
        clips = {c.story_id: c for c in ensure_clips(session, specs, providers,
                                                     synthesize=synthesize)}
        bad = {c.story.id for c in chosen
               if c.story.id not in clips or not clips[c.story.id].verified}
        excluded |= bad  # type: ignore[arg-type]
        for c in candidates:  # refine estimates with real (or word-based) clip lengths
            clip = clips.get(c.story.id) or find_clip(session, c.story.id, plan.tone,  # type: ignore[arg-type]
                                                      plan.language, plan.voice)
            if clip and clip.verified:
                c.est_seconds = clip.duration_sec or speech_seconds(clip.script, plan.language)
        if not bad and [c.story.id for c in chosen] == [c.story.id for c in previous]:
            break
    chosen = [c for c in chosen if c.story.id in clips and clips[c.story.id].verified]
    if not chosen:
        return None

    plan.items = [(c, clips[c.story.id]) for c in chosen]  # type: ignore[index]
    seen_topics: dict[int, Topic] = {}
    for c in chosen:
        seen_topics.setdefault(c.topic.id, c.topic)  # type: ignore[arg-type]
    plan.topics = list(seen_topics.values())
    plan.sources = [{"headline": c.story.headline, "source_name": c.story.source_name,
                     "url": c.story.canonical_url, "topic": c.topic.key} for c in chosen]
    plan.est_seconds = sum(c.est_seconds for c in chosen) + cfg.frame_overhead_seconds
    return plan


def episode_image(session: Session, user_id: int, topics: list[Topic]) -> str | None:
    """The catalog picture of the episode's first topic that has one (a subtopic such as
    `ai` uses its parent's, `technology`)."""
    interests = {ut.topic_id: ut.interest for ut in session.exec(
        select(UserTopic).where(UserTopic.user_id == user_id))}
    images = interest_images()
    for topic in topics:
        parent = session.get(Topic, topic.parent_id) if topic.parent_id else None
        for slug in (interests.get(topic.id, ""), topic.key, parent.key if parent else ""):
            if images.get(slug):
                return images[slug]
    return None


def reviewed_plan(session: Session, user: User, providers: Providers, now: datetime,
                  day: date, topic: str | None, first_name: str
                  ) -> tuple[EpisodePlan | None, dict[str, Any]]:
    """Plan (text only), then the accuracy review (review.py): segments it flags are removed
    and the episode re-planned, at most review.rounds times; flagged teasers are dropped and
    forgotten. Returns the final plan (frame written) and what the review found."""
    from app.pipeline.review import forget_teasers, review_plan

    rounds = pipeline_cfg().assemble.review.rounds
    excluded: set[int] = set()
    skip_teasers: set[int] = set()
    found: list[dict[str, str]] = []
    ref = f"user:{user.id}"
    for attempt in range(rounds + 1):
        plan = plan_episode(session, user, providers, now, synthesize=False, day=day,
                            only=topic, exclude=excluded)
        if plan is None:
            return None, {}
        plan.skip_teasers = skip_teasers
        frame_for(session, providers, plan, first_name, day, ref, synthesize=False)
        result = review_plan(session, providers, plan, ref)
        found += result.issues
        bad, teasers = result.story_ids(), result.teaser_ids()
        if teasers:
            forget_teasers(session, teasers)
            skip_teasers |= teasers
        if not bad and not teasers:
            break
        if attempt == rounds:  # out of rounds: drop what is still flagged, no new stories
            plan.items = [(c, clip) for c, clip in plan.items if c.story.id not in bad]
            if not plan.items:
                return None, {}
            plan.topics = [t for t in plan.topics
                           if any(c.topic.id == t.id for c, _ in plan.items)]
            frame_for(session, providers, plan, first_name, day, ref, synthesize=False)
            break
        excluded |= bad
        for issue in result.issues:
            log.warning("Review, user %s: %s %s: %s", user.id, issue["type"],
                        issue["segment"], issue["detail"])
    plan.sources = [{"headline": c.story.headline, "source_name": c.story.source_name,
                     "url": c.story.canonical_url, "topic": c.topic.key} for c, _ in plan.items]
    return plan, {"reviewed": pipeline_cfg().assemble.review.enabled, "rounds": attempt + 1,
                  "issues": found, "removed": sorted(excluded),
                  "teasers_dropped": sorted(skip_teasers), "clean_first_time": not found}


def assemble_episode(session: Session, user_id: int, providers: Providers,
                     now: datetime | None = None, dry_run: bool = False,
                     publish_at: datetime | None = None,
                     topic: str | None = None) -> Episode | EpisodePlan | None:
    """Build today's episode for one user: the daily briefing, or with `topic` (an interest
    slug the user follows) that topic's own episode. Returns None (and logs) if nothing new.

    publish_at: when the episode becomes visible (its delivery time; episodes are built
    ahead of it). dry_run stops before TTS and stitching and creates no episode (stories stay
    unseen); shared clip and phrase text may still be cached.
    """
    now = now or utcnow()
    started = time.monotonic()  # assembly time: planning, scripts on demand, review, audio
    user = session.get(User, user_id)
    if user is None:
        raise ValueError(f"user {user_id} not found")
    day = local_day(current_profile(session, user_id), publish_at or now)
    first_name = (user.name or "").split(" ")[0]
    # Plan on text only, review the whole episode, and record audio only for what survives
    plan, quality = reviewed_plan(session, user, providers, now, day, topic, first_name)
    if plan is None:
        log.info("User %s: no unseen stories%s; skipping episode", user_id,
                 f" on {topic}" if topic else "")
        return None

    if dry_run:
        plan.transcript = build_transcript(plan)
        plan.frame["quality"] = quality
        session.commit()
        return plan
    ensure_clips(session, [(c.story, plan.tone, plan.language, plan.voice)
                           for c, _ in plan.items], providers, synthesize=True)

    cfg = pipeline_cfg().assemble
    if topic:
        label = interest_labels().get(topic, plan.topics[0].display_name)
        title = f"Your {label} update, {now:%A %B} {now.day}"
    else:
        title = f"Your Hearline briefing, {now:%A %B} {now.day}"
    description = "Today: " + "; ".join(s["headline"] for s in plan.sources)
    episode = Episode(user_id=user_id, topic=topic or "briefing", title=title,
                      image_url=episode_image(session, user_id, plan.topics),
                      description=description[:1000], status="generating",
                      published_at=publish_at or now, quality=quality,
                      tone=plan.tone, language=plan.language, voice=plan.voice)
    session.add(episode)
    session.commit()
    ref = f"episode:{episode.id}"
    try:
        opening, transitions, outro = frame_for(session, providers, plan, first_name,
                                                now.date(), ref, synthesize=True)
        plan.transcript = build_transcript(plan)
        audio_dir, storage = get_settings().audio_dir, get_storage()
        # Optional royalty-free sounds (backend sounds/ folder) frame the voice: sting, whoosh, tail
        intro_sfx, transition_sfx, outro_sfx = sound("intro"), sound("transition"), sound("outro")
        fetch = storage.fetch
        parts = [intro_sfx] + [fetch(p.audio_path) for p in opening]  # type: ignore[arg-type]
        for topic, transition in zip(plan.topics, transitions, strict=True):
            parts += [transition_sfx, fetch(transition.audio_path)]  # type: ignore[arg-type]
            parts += [fetch(clip.audio_path) for cand, clip in plan.items  # type: ignore[arg-type]
                      if cand.topic.id == topic.id]  # S3: download
        parts += [fetch(outro.audio_path), outro_sfx]  # type: ignore[arg-type]
        parts = [p for p in parts if p is not None]  # a missing sound is just skipped

        user_dir = audio_dir / "users" / str(user_id) / "episodes"
        out, seconds = stitch(parts, user_dir / str(episode.id), title,
                              description, cfg.gap_ms, cfg.crossfade_ms, cfg.target_dbfs,
                              cfg.sfx_offset_db, cfg.export_format, cfg.mp3_bitrate,
                              cfg.sfx_offsets_db, cfg.topic_pause_ms)

        episode.frame = plan.frame
        episode.audio_path = str(out.relative_to(audio_dir))
        storage.save(episode.audio_path)
        episode.duration_seconds = round(seconds)
        episode.audio_bytes = out.stat().st_size
        episode.status = "ready"
        episode.ready_at = utcnow()
        assembly = round(time.monotonic() - started, 1)
        episode.timing = {"assembly": assembly}
        episode.generation_seconds = assembly  # the scheduler adds research and clip jobs
        for pos, (cand, _) in enumerate(plan.items):
            session.add(EpisodeStory(episode_id=episode.id, story_id=cand.story.id,  # type: ignore[arg-type]
                                     position=pos, user_id=user_id, created_at=now))
        session.add(episode)
        session.commit()
    except Exception:
        session.rollback()
        episode.status = "failed"
        session.add(episode)
        session.commit()
        raise
    log.info("Episode %s for user %s: %d stories, %.0fs", episode.id, user_id,
             len(plan.items), seconds)
    return episode
