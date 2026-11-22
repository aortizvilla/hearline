"""Step 1: topic registry. Every topic a user selects is classified here, as cheaply as possible.

Entry points: set_interests (Settings page, PUT /api/profile: replaces the user's topics) and
subscribe (adds one interest). Both classify each choice with resolve_interest:
1. exact alias match on the normalized phrase (free; every catalog topic is a seeded alias)
2. nearest topic by embedding similarity above `topics.match_threshold` (local, free)
3. ONE cheap LLM call deciding same_as / child_of / new (SMALL_MODEL, prompts/topic_resolve.txt)
Each decision is stored as an alias, so a phrase never costs again. Niche phrases become a
keyword filter on a broader topic. After a selection, topic subscriber counts are refreshed so
step 2 researches the new topic on its next run.

Subcategories (resolve_subtopic): a listener can add up to `topics.max_subtopics` focus phrases
per topic ("Film" > "Cannes Festival"). Each is classified into a shared Subtopic of that topic
the same cheap way (known phrase -> embedding match -> ONE small-model call, prompt
prompts/subtopic_resolve.txt, which also rejects phrases that don't belong). Stories are tagged
with the subtopics they mention; research is told which ones listeners follow; matching stories
rank higher in step 4.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path

import yaml
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, func, select

from app.config import CONFIG_DIR, interests_cfg, language_names, load_prompt, pipeline_cfg
from app.database.models import (
    CostEvent,
    Story,
    Subtopic,
    Topic,
    TopicAlias,
    User,
    UserTopic,
    utcnow,
)
from app.providers import Providers, build_providers
from app.providers.embeddings import EmbeddingProvider
from app.providers.llm import LLMError, LLMRequest, extract_json
from app.tracking.costs import record_llm, spent_today

log = logging.getLogger(__name__)
SEED_FILE = CONFIG_DIR / "seed_topics.yaml"
SUBTOPICS_FILE = CONFIG_DIR / "subtopics.yaml"


@dataclass
class Resolution:
    topic: Topic
    keyword: str | None  # niche phrase to apply as a keyword filter on `topic`
    method: str  # alias | embedding | llm


def normalize(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[^a-z0-9+#]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalize(text)).strip("-")[:60] or "topic"


def _topic_text(display_name: str, aliases: list[str]) -> str:
    return f"{display_name}: {', '.join(aliases[:8])}" if aliases else display_name


def _add_alias(session: Session, topic: Topic, alias: str, source: str,
               keyword: str | None = None) -> None:
    """Insert an alias unless it already exists (concurrency-safe)."""
    if session.exec(select(TopicAlias).where(TopicAlias.alias_normalized == alias)).first():
        return
    try:
        with session.begin_nested():
            session.add(TopicAlias(topic_id=topic.id, alias_normalized=alias, source=source,
                                   keyword=keyword))
    except IntegrityError:
        pass  # another worker stored the same decision


def seed_topics(session: Session, embedder: EmbeddingProvider, path: Path = SEED_FILE) -> int:
    """Create seed topics + aliases (idempotent). Returns the number of new topics."""
    entries = yaml.safe_load(path.read_text(encoding="utf-8"))
    existing = {t.key: t for t in session.exec(select(Topic))}
    new = [e for e in entries if e["key"] not in existing]
    vectors = embedder.embed(
        [_topic_text(e["display_name"], e.get("aliases", [])) for e in new]
    ) if new else []
    for entry, vec in zip(new, vectors, strict=True):
        topic = Topic(key=entry["key"], display_name=entry["display_name"], embedding=vec)
        session.add(topic)
        existing[topic.key] = topic
    session.flush()
    known_aliases = set(session.exec(select(TopicAlias.alias_normalized)).all())
    for entry in entries:
        topic = existing[entry["key"]]
        if entry.get("parent") and topic.parent_id is None:
            topic.parent_id = existing[entry["parent"]].id
        phrases = {entry["key"].replace("-", " "), entry["display_name"],
                   *entry.get("aliases", [])}
        for alias in {normalize(p) for p in phrases} - known_aliases:
            session.add(TopicAlias(topic_id=topic.id, alias_normalized=alias, source="seed"))
            known_aliases.add(alias)
    session.flush()
    for item in interests_cfg():  # Settings names per language -> dim_topic.labels
        alias = session.exec(select(TopicAlias).where(
            TopicAlias.alias_normalized == normalize(item["slug"]))).first()
        topic = session.get(Topic, alias.topic_id) if alias else None
        labels = {"en": item["label"], **(item.get("labels") or {})}
        if topic is not None and topic.labels != labels:
            topic.labels = labels
            session.add(topic)
    session.commit()
    return len(new)


# -- the per-listener limit on LLM classification (Settings saves) ------------------------
class SubtopicError(ValueError):
    """A subcategory phrase that can't be used (doesn't fit the topic, empty, too long)."""


class PhraseLimitError(SubtopicError):
    """A listener's new phrases need the LLM, but today's allowance (or budget) is used up."""


_listener: ContextVar[int | None] = ContextVar("classifying_for", default=None)
_LIMITED = ("topic_resolve", "subtopic_resolve")


def _ref(ref: str) -> str:
    """Cost reference, tagged with the listener the call was made for (to count the limit)."""
    uid = _listener.get()
    return f"user:{uid}:{ref}" if uid is not None else ref


def _check_allowance(session: Session) -> None:
    """Before an LLM classification made for a listener: refuse past their daily allowance or
    once the pipeline's daily budget is spent. Background work (no listener) is never limited."""
    uid = _listener.get()
    if uid is None:
        return
    now = utcnow()
    midnight = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
    used = session.exec(select(func.count(col(CostEvent.id))).where(
        col(CostEvent.stage).in_(_LIMITED), col(CostEvent.ref_id).startswith(f"user:{uid}:"),
        CostEvent.created_at >= midnight)).one()
    if used >= pipeline_cfg().topics.max_new_phrases_per_day:
        raise PhraseLimitError("You've added a lot of new topics and subcategories today. "
                               "Pick from the list, or try again tomorrow.")
    if spent_today(session, now) >= pipeline_cfg().scheduler.daily_budget_usd:
        raise PhraseLimitError("New topics and subcategories can't be added right now. "
                               "Pick from the list, or try again later.")


def _llm_decide(session: Session, providers: Providers, text: str,
                candidates: list[tuple[Topic, float]]) -> dict[str, object] | None:
    cand_payload = [
        {"key": t.key, "display_name": t.display_name, "similarity": round(s, 3)}
        for t, s in candidates
    ]
    req = LLMRequest(
        purpose="topic_resolve",
        model=providers.small_model,  # the cheap model
        system=load_prompt("topic_resolve.txt"),
        messages=[{"role": "user", "content": json.dumps(
            {"phrase": text, "closest_existing_topics": cand_payload})}],
        max_tokens=1500,
        effort="low",
        context={"text": text, "candidates": cand_payload},
    )
    _check_allowance(session)
    try:
        resp = providers.llm.complete(req)
    except LLMError as exc:
        log.warning("Topic resolution LLM call failed for %r: %s", text, exc)
        return None
    record_llm(session, "topic_resolve", _ref(f"alias:{normalize(text)}"), resp)
    try:
        decision = extract_json(resp.text)
        return decision if isinstance(decision, dict) else None
    except ValueError:
        log.warning("Unparseable topic decision for %r: %r", text, resp.text[:200])
        return None


def resolve_interest(session: Session, user_text: str,
                     providers: Providers | None = None) -> Resolution:
    """Map free text to a topic (and optional keyword filter), caching every decision.

    providers are only needed (and built from settings if not given) when no alias matches."""
    cfg = pipeline_cfg().topics
    norm = normalize(user_text)
    if not norm:
        raise ValueError("empty interest")

    alias = session.exec(select(TopicAlias).where(TopicAlias.alias_normalized == norm)).first()
    if alias:
        return Resolution(session.get(Topic, alias.topic_id), alias.keyword, "alias")  # type: ignore[arg-type]

    providers = providers or build_providers()
    vector = providers.embedder.embed_one(norm)
    candidates = providers.vectors.nearest_topics(session, vector, cfg.llm_candidates)
    if candidates and candidates[0][1] >= cfg.match_threshold:
        topic = candidates[0][0]
        _add_alias(session, topic, norm, "embedding")
        session.commit()
        return Resolution(topic, None, "embedding")

    decision = _llm_decide(session, providers, user_text.strip(), candidates) or {}
    by_key = {t.key: t for t, _ in candidates}

    def known(key: object) -> Topic | None:
        if not isinstance(key, str):
            return None
        return by_key.get(key) or session.exec(select(Topic).where(Topic.key == key)).first()

    keyword: str | None = None
    topic: Topic | None = None
    if (t := known(decision.get("same_as"))) is not None:
        topic = t
    elif (parent := known(decision.get("child_of"))) is not None:
        if cfg.niche_as_keyword:
            topic, keyword = parent, norm
        else:
            topic = _create_topic(session, providers, slugify(user_text), user_text.strip(),
                                  parent.id)
    elif isinstance(decision.get("new"), dict):
        new = decision["new"]
        key = slugify(str(new.get("key") or user_text))  # type: ignore[union-attr]
        name = str(new.get("display_name") or user_text.strip().title())  # type: ignore[union-attr]
        topic = known(key) or _create_topic(session, providers, key, name, None)
    elif candidates and candidates[0][1] >= cfg.match_threshold - 0.2:
        topic = candidates[0][0]  # LLM unavailable: fall back to the nearest close topic
    else:
        topic = _create_topic(session, providers, slugify(user_text), user_text.strip().title(),
                              None)

    _add_alias(session, topic, norm, "llm", keyword)
    session.commit()
    return Resolution(topic, keyword, "llm")


def _create_topic(session: Session, providers: Providers, key: str, display_name: str,
                  parent_id: int | None) -> Topic:
    existing = session.exec(select(Topic).where(Topic.key == key)).first()
    if existing:
        return existing
    topic = Topic(key=key, display_name=display_name, parent_id=parent_id,
                  embedding=providers.embedder.embed_one(display_name))
    session.add(topic)
    session.flush()
    _add_alias(session, topic, normalize(display_name), "llm")
    log.info("Created topic %s (%s)", key, display_name)
    return topic


def resolve_topic(session: Session, user_text: str, providers: Providers) -> Topic:
    """Return the shared Topic for a free-text interest."""
    return resolve_interest(session, user_text, providers).topic


def subscribe(session: Session, user_id: int, user_text: str, providers: Providers,
              weight: float = 1.0) -> UserTopic:
    """Subscribe a user to an interest. Niche interests become keyword filters.

    Empty keyword_filters means "every story of the topic"; a broad subscription therefore
    clears filters, and a niche one only adds a filter when the user is not already broad.
    """
    res = resolve_interest(session, user_text, providers)
    ut = session.exec(
        select(UserTopic).where(UserTopic.user_id == user_id, UserTopic.topic_id == res.topic.id)
    ).first()
    if ut is None:
        ut = UserTopic(user_id=user_id, topic_id=res.topic.id, weight=weight,  # type: ignore[arg-type]
                       interest=user_text, keyword_filters=[res.keyword] if res.keyword else [])
    elif res.keyword is None:
        ut.keyword_filters, ut.weight = [], max(ut.weight, weight)
    elif ut.keyword_filters and res.keyword not in ut.keyword_filters:
        ut.keyword_filters = [*ut.keyword_filters, res.keyword]
    session.add(ut)
    session.commit()
    return ut


def seed_subtopics(session: Session, embedder: EmbeddingProvider,
                   path: Path = SUBTOPICS_FILE) -> int:
    """Load each topic's suggested subcategories (idempotent). A subcategory a listener already
    created with the same key becomes suggested; its keywords are merged. Returns new count."""
    entries = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    topics = {t.key: t for t in session.exec(select(Topic).where(
        col(Topic.key).in_(list(entries))))}
    existing = {(s.topic_id, s.key): s for s in session.exec(select(Subtopic).where(
        col(Subtopic.topic_id).in_([t.id for t in topics.values()])))}
    new: list[Subtopic] = []
    for topic_key, items in entries.items():
        topic = topics.get(topic_key)
        if topic is None:
            log.warning("subtopics.yaml: unknown topic %r", topic_key)
            continue
        for order, item in enumerate(items):
            key = item.get("key") or slugify(item["label"])
            terms = [normalize(k) for k in item.get("keywords", [])]
            sub = existing.get((topic.id, key))  # type: ignore[arg-type]
            if sub is None:
                sub = Subtopic(topic_id=topic.id, key=key, label=item["label"])  # type: ignore[arg-type]
                new.append(sub)
            sub.label, sub.suggested, sub.sort_order = item["label"], True, order
            sub.labels = {"en": item["label"], **(item.get("labels") or {})}
            sub.keywords = list(dict.fromkeys([*terms, *(sub.keywords or [])]))
            sub.aliases = list(dict.fromkeys([*(sub.aliases or []), normalize(item["label"])]))
            session.add(sub)
    if new:
        vectors = embedder.embed([normalize(s.label) for s in new])
        for sub, vec in zip(new, vectors, strict=True):
            sub.embedding = vec
    session.commit()
    return len(new)


def suggested_subtopics(session: Session) -> dict[int, list[dict[str, object]]]:
    """Topic id -> its suggested subcategories in order, each {"label", "labels"} where
    labels is the name per interface language (for Settings)."""
    out: dict[int, list[dict[str, object]]] = {}
    for sub in session.exec(select(Subtopic).where(Subtopic.suggested == True)  # noqa: E712
                            .order_by(col(Subtopic.topic_id), col(Subtopic.sort_order))):
        out.setdefault(sub.topic_id, []).append(
            {"label": sub.label, "labels": sub.labels or {"en": sub.label}})
    return out


def story_mentions(story: Story, terms: list[str]) -> bool:
    """True if any term appears as whole words in the story's headline or summary."""
    text = f" {normalize(story.headline)} {normalize(story.summary)} "
    return any(f" {normalize(t)} " in text for t in terms if normalize(t))


def tag_stories(stories: list[Story], subtopics: list[Subtopic]) -> None:
    """Classify stories into their topic's subtopics (free keyword match, no model call)."""
    for story in stories:
        keys = [s.key for s in subtopics if s.topic_id == story.topic_id
                and story_mentions(story, [s.label, *s.keywords])]
        if keys != (story.subtopics or []):
            story.subtopics = keys


def _llm_subtopic(session: Session, providers: Providers, topic: Topic, phrase: str,
                  existing: list[Subtopic]) -> dict[str, object]:
    payload = {"topic": topic.display_name, "phrase": phrase,
               "existing_subcategories": [{"key": s.key, "label": s.label} for s in existing],
               "label_languages": [code for code in language_names() if code != "en"]}
    req = LLMRequest(
        purpose="subtopic_resolve", model=providers.small_model,
        system=load_prompt("subtopic_resolve.txt"),
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        max_tokens=1500, effort="low", context=payload,
    )
    _check_allowance(session)
    try:
        resp = providers.llm.complete(req)
    except LLMError as exc:
        log.warning("Subtopic classification failed for %r: %s", phrase, exc)
        return {}
    record_llm(session, "subtopic_resolve", _ref(f"topic:{topic.key}"), resp)
    try:
        decision = extract_json(resp.text)
    except ValueError:
        return {}
    return decision if isinstance(decision, dict) else {}


def resolve_subtopic(session: Session, topic: Topic, phrase: str,
                     providers: Providers | None = None) -> Subtopic:
    """Classify a listener's focus phrase into a shared subtopic of `topic` (created if new).
    Raises SubtopicError when the phrase doesn't fit the topic."""
    norm = normalize(phrase)
    if not 2 <= len(norm) <= 60:
        raise SubtopicError(f"Subcategory {phrase!r} must be 2-60 characters.")
    existing = list(session.exec(select(Subtopic).where(Subtopic.topic_id == topic.id)))
    for s in existing:  # 1. a phrase seen before: free
        if norm in (s.aliases or []):
            return s

    def remember(s: Subtopic) -> Subtopic:
        if norm not in (s.aliases or []):
            s.aliases = [*(s.aliases or []), norm]
            session.add(s)
            session.flush()
        return s

    providers = providers or build_providers()
    vector = providers.embedder.embed_one(norm)  # 2. same meaning as an existing one: free
    scored = [(sum(a * b for a, b in zip(vector, s.embedding, strict=True)), s)
              for s in existing if s.embedding and len(s.embedding) == len(vector)]
    if scored:
        score, best = max(scored, key=lambda x: x[0])
        if score >= pipeline_cfg().topics.match_threshold:
            return remember(best)

    decision = _llm_subtopic(session, providers, topic, phrase.strip(), existing)  # 3. one call
    if decision.get("unrelated"):
        raise SubtopicError(f"“{phrase.strip()}” doesn't fit {topic.display_name}.")
    by_key = {s.key: s for s in existing}
    if isinstance(decision.get("same_as"), str) and decision["same_as"] in by_key:
        return remember(by_key[decision["same_as"]])
    new = decision.get("new") if isinstance(decision.get("new"), dict) else {}
    label = str(new.get("label") or phrase.strip()).strip()[:80]  # type: ignore[union-attr]
    raw_terms = new.get("keywords") if isinstance(new.get("keywords"), list) else []  # type: ignore[union-attr]
    terms = list(dict.fromkeys(t for t in (normalize(str(k)) for k in [*raw_terms, norm]) if t))
    key = slugify(label)
    if key in by_key:
        return remember(by_key[key])
    raw_labels = new.get("labels") if isinstance(new.get("labels"), dict) else {}  # type: ignore[union-attr]
    labels = {"en": label, **{str(k): str(v).strip()[:80] for k, v in raw_labels.items()  # type: ignore[union-attr]
                              if k in language_names() and k != "en" and str(v).strip()}}
    subtopic = Subtopic(topic_id=topic.id, key=key, label=label, keywords=terms[:10], labels=labels,  # type: ignore[arg-type]
                        aliases=list(dict.fromkeys([norm, normalize(label)])),
                        embedding=providers.embedder.embed_one(normalize(label)))
    session.add(subtopic)
    session.flush()
    since = utcnow() - timedelta(hours=pipeline_cfg().assemble.weekly_lookback_hours)
    recent = list(session.exec(select(Story).where(Story.topic_id == topic.id,
                                                   col(Story.first_seen) >= since)))
    tag_stories(recent, [*existing, subtopic])  # this week's stories get the new tag too
    for story in recent:
        session.add(story)
    log.info("New subtopic %s > %s", topic.key, label)
    return subtopic


def set_interests(session: Session, user_id: int,
                  interests: list[tuple[str, str, int | None]]
                  | list[tuple[str, str, int | None, list[str]]],
                  providers: Providers | None = None) -> list[UserTopic]:
    """Replace a user's subscriptions with the Settings page's choices:
    (interest, frequency, minutes[, subcategory phrases]). Each choice is classified by
    resolve_interest, so a catalog slug and free text take the same path; each subcategory by
    resolve_subtopic (SubtopicError if one doesn't fit). Topics not chosen anymore are removed.
    New phrases that need the LLM count against the listener's daily allowance
    (PhraseLimitError past it)."""
    token = _listener.set(user_id)
    try:
        return _set_interests(session, user_id, interests, providers)
    finally:
        _listener.reset(token)


def _set_interests(session: Session, user_id: int, interests: list, providers: Providers | None
                   ) -> list[UserTopic]:
    chosen: dict[int, UserTopic] = {}
    current = {ut.topic_id: ut for ut in session.exec(
        select(UserTopic).where(UserTopic.user_id == user_id))}
    max_subtopics = pipeline_cfg().topics.max_subtopics
    for interest, frequency, minutes, *rest in interests:
        phrases = list(rest[0]) if rest else []
        if len(phrases) > max_subtopics:
            raise SubtopicError(f"At most {max_subtopics} subcategories per topic.")
        res = resolve_interest(session, interest, providers)
        subtopic_keys = list(dict.fromkeys(
            resolve_subtopic(session, res.topic, p, providers).key for p in phrases))
        topic_id: int = res.topic.id  # type: ignore[assignment]
        keywords = [res.keyword] if res.keyword else []
        if topic_id in chosen:  # two choices map to one topic: broad wins, else keep both
            ut = chosen[topic_id]
            ut.keyword_filters = (ut.keyword_filters and keywords
                                  and sorted({*ut.keyword_filters, *keywords})) or []
            continue
        ut = current.get(topic_id) or UserTopic(user_id=user_id, topic_id=topic_id)
        ut.interest, ut.frequency, ut.target_minutes = interest, frequency, minutes
        ut.weight = (minutes or 10) / 10  # more minutes -> more of that topic's stories
        ut.keyword_filters = keywords
        ut.subtopics = subtopic_keys
        session.add(ut)
        chosen[topic_id] = ut
    for topic_id, ut in current.items():
        if topic_id not in chosen:
            session.delete(ut)
    session.commit()
    refresh_active_subscribers(session)
    return list(chosen.values())


def refresh_active_subscribers(session: Session, now: datetime | None = None) -> dict[str, int]:
    """Recount subscribers per topic among users active in the last N days."""
    now = now or utcnow()
    cutoff = now - timedelta(days=pipeline_cfg().scheduler.inactive_days)
    rows = session.exec(
        select(UserTopic.topic_id, func.count(func.distinct(UserTopic.user_id)))
        .join(User, col(User.id) == UserTopic.user_id)
        .where(col(User.last_active_at) >= cutoff)
        .group_by(UserTopic.topic_id)
    ).all()
    counts = dict(rows)
    result: dict[str, int] = {}
    for topic in session.exec(select(Topic)):
        topic.active_subscribers = int(counts.get(topic.id, 0))
        session.add(topic)
        if topic.active_subscribers:
            result[topic.key] = topic.active_subscribers
    session.commit()
    return result


def keyword_hints(session: Session, topic_id: int, limit: int) -> list[str]:
    """What subscribers of a topic care about most, for the shared research call: their
    subcategories (by label) and niche keyword filters, most common first."""
    labels = {s.key: s.label for s in session.exec(
        select(Subtopic).where(Subtopic.topic_id == topic_id))}
    counter: Counter[str] = Counter()
    for filters, subtopics in session.exec(
        select(UserTopic.keyword_filters, UserTopic.subtopics)
        .where(UserTopic.topic_id == topic_id)
    ):
        counter.update(filters or [])
        counter.update(labels[k] for k in (subtopics or []) if k in labels)
    return [k for k, _ in counter.most_common(limit)]
