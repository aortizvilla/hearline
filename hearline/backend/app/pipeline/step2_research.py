"""Step 2: shared research, once per active topic per day and research language.

RESEARCHER_MODEL + web search (prompt: prompts/research_system.txt) -> JSON stories ->
Pydantic validation (inline citations stripped) -> lookback filter -> URL canonicalization ->
dedupe (canonical URL; a story whose other sources include a known story is the same event;
then headline embedding) -> relevance check (one SMALL_MODEL call drops stories that aren't
about the topic) -> plural sourcing (at most `research.max_per_source` stories per outlet; other
outlets reporting a story kept as its sources) -> link check (section/tag pages and dead links
dropped) -> Wikipedia background for terms a listener may not know -> insert new Story rows.

Languages: every topic is researched in English (international outlets), plus in each
language of `research.languages` its active listeners use, searching that language's own
outlets (e.g. El Pais, Clarin for Spanish). Local research is told today's English headlines so
it adds local stories instead of repeating them. Cost grows with topics x languages in use.

The stored headline, summary, why_it_matters, other sources and background are the only facts
step 3 may use.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlmodel import Session, col, select

from app.config import language_names, load_prompt, pipeline_cfg
from app.database.models import Story, Subtopic, Topic, as_utc, utcnow
from app.pipeline.step1_topics import keyword_hints, tag_stories
from app.providers import Providers, web
from app.providers.llm import (
    LLMError,
    LLMRequest,
    LLMResponse,
    TransientLLMError,
    WebSearchSpec,
    extract_json,
    with_retries,
)
from app.tracking.costs import record_llm

log = logging.getLogger(__name__)

TRACKING_PARAMS = re.compile(
    r"^(utm_.*|fbclid|gclid|dclid|mc_cid|mc_eid|ref|ref_src|ref_url|cmpid|cmp|ocid|smid|"
    r"src|source|share|s_cid|igshid|at_medium|at_campaign|guccounter|taid|mbid|ito)$",
    re.I,
)
# Web search tools (OpenAI in particular) append inline citations such as
# " ([apnews.com](https://...))" to text fields. The story's URL is stored separately,
# and these fields are later spoken, so citations are removed.
CITATION_GROUP = re.compile(r"\s*\(\s*(?:\[[^\]]*\]\([^)]*\)[\s,;]*)+\)")
MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
BARE_URL = re.compile(r"\s*\(?https?://[^\s)]*[^\s).,;:!?]\)?")


def strip_citations(text: str) -> str:
    """Remove markdown links and URLs from model text, keeping link labels."""
    text = CITATION_GROUP.sub("", text)
    text = MD_LINK.sub(r"\1", text)
    text = BARE_URL.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip()


class RawStory(BaseModel):
    """One story as returned by the researcher model."""

    headline: str = Field(min_length=5, max_length=300)
    summary: str = Field(min_length=20, max_length=1500)
    why_it_matters: str = Field(default="", max_length=600)
    source_name: str = Field(min_length=2, max_length=120)
    url: str
    published_date: str = Field(min_length=8)
    importance: int = 5
    other_sources: list[dict[str, Any]] = Field(default_factory=list)  # [{source_name, url}]
    background_terms: list[str] = Field(default_factory=list)  # for Wikipedia context

    @field_validator("headline", "summary", "why_it_matters", mode="before")
    @classmethod
    def _no_citations(cls, v: Any) -> Any:
        return strip_citations(v) if isinstance(v, str) else v

    @field_validator("source_name", mode="before")
    @classmethod
    def _outlet(cls, v: Any) -> Any:
        """The outlet only: "Gulf Times (Reuters report)" -> "Gulf Times" (scripts name it)."""
        if not isinstance(v, str):
            return v
        clean = re.sub(r"\s*\([^)]*\)\s*", " ", strip_citations(v)).strip()
        return clean or v.strip()

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^https?://[^\s/]+\.[^\s/]+", v):
            raise ValueError("missing or invalid url")
        return v

    @field_validator("importance", mode="before")
    @classmethod
    def _importance(cls, v: Any) -> int:
        try:
            return max(1, min(10, int(float(v))))
        except (TypeError, ValueError):
            return 5


class ResearchPayload(BaseModel):
    stories: list[dict[str, Any]]


@dataclass
class ResearchResult:
    topic_key: str
    language: str = "en"
    new_story_ids: list[int] = field(default_factory=list)
    returned: int = 0
    invalid: int = 0
    out_of_window: int = 0
    duplicates: int = 0
    updates: int = 0
    over_source_cap: int = 0  # dropped: too many stories from one outlet
    dead_links: int = 0  # dropped: not an article link (section page), or it doesn't exist
    off_topic: int = 0  # dropped: the relevance check says it isn't about the topic
    skipped: bool = False


def canonicalize_url(url: str) -> str:
    """Normalize a URL so the same article always maps to the same key."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    host = host[2:] if host.startswith("m.") else host
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    path = re.sub(r"/(amp|output=amp)/?$", "", path)
    if len(path) > 1:
        path = path.rstrip("/")
    query = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not TRACKING_PARAMS.match(k)
    )
    return urlunsplit(("https", host, path, urlencode(query), ""))


def parse_published(value: str) -> tuple[datetime, bool]:
    """Parse a date or datetime string. Returns (UTC datetime, is_date_only)."""
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return datetime.combine(date.fromisoformat(value), time(12), tzinfo=UTC), True
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)), False
    except ValueError:
        pass
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(value, fmt).replace(hour=12, tzinfo=UTC), True
        except ValueError:
            continue
    raise ValueError(f"unparseable date {value!r}")


def in_window(published: datetime, date_only: bool, now: datetime, lookback_hours: int) -> bool:
    start = now - timedelta(hours=lookback_hours)
    if date_only:
        return start.date() <= published.date() <= now.date()
    return start <= published <= now + timedelta(hours=1)


def build_request(session: Session, topic: Topic, providers: Providers, lookback_hours: int,
                  now: datetime, language: str = "en") -> LLMRequest:
    cfg = pipeline_cfg()
    src = cfg.research.search
    hints = keyword_hints(session, topic.id, cfg.topics.research_keyword_hints)  # type: ignore[arg-type]
    start = now - timedelta(hours=lookback_hours)
    lines = [
        f"Topic: {topic.display_name}",
        f"Time window: {start:%Y-%m-%d %H:%M} UTC to {now:%Y-%m-%d %H:%M} UTC "
        f"(today is {now:%A, %B %d, %Y}).",
        f"Return between {cfg.research.min_stories} and {cfg.research.max_stories} stories "
        "(fewer only if there is not enough real news).",
        f"Use at most {cfg.research.max_per_source} stories from any one outlet.",
    ]
    local = cfg.research.languages.get(language) if language != "en" else None
    if local is not None:
        name = language_names().get(language, language)
        lines.append(
            f"Research language: {name}. Search in {name} and use news published in {name} by "
            f"outlets from {local.regions or 'where it is spoken'}"
            + (f", such as {', '.join(local.outlets)}, and other reputable local media"
               if local.outlets else "") + ". Write headline, summary and why_it_matters in "
            f"{name}.")
        covered = session.exec(select(Story.headline).where(
            Story.topic_id == topic.id, Story.language != language,
            col(Story.first_seen) >= start)).all()
        if covered:
            lines.append("Already covered today in other languages (skip these events; add "
                         "local stories instead): " + " | ".join(covered[:15]))
    elif cfg.research.english_outlets:
        lines.append("Draw from a range of outlets, e.g. " + ", ".join(cfg.research.english_outlets)
                     + ", and specialist or regional media.")
    if hints:
        lines.append("Listeners of this topic are especially interested in: "
                     + ", ".join(hints) + ". Include such stories when they are newsworthy.")
    return LLMRequest(
        purpose="research",
        model=providers.researcher_model,
        system=load_prompt("research_system.txt"),
        messages=[{"role": "user", "content": "\n".join(lines)}],
        max_tokens=cfg.research.llm.max_tokens,
        effort=cfg.research.llm.effort,
        refusal_fallbacks=cfg.research.llm.refusal_fallbacks,
        web_search=WebSearchSpec(src.max_uses, src.allowed_domains, src.blocked_domains),
        context={"topic_key": topic.key, "display_name": topic.display_name, "language": language,
                 "now": now.isoformat(), "lookback_hours": lookback_hours, "keywords": hints},
    )


def _parse(text: str) -> list[dict[str, Any]]:
    data = extract_json(text)
    if isinstance(data, list):
        data = {"stories": data}
    return ResearchPayload.model_validate(data).stories


def _parse_with_fix(session: Session, topic: Topic, providers: Providers, req: LLMRequest,
                    resp: LLMResponse) -> list[dict[str, Any]]:
    """Parse JSON; on failure make ONE corrective call (no tools, cheap) and parse again."""
    try:
        return _parse(resp.text)
    except (ValueError, ValidationError) as exc:
        log.warning("Research JSON invalid for %s (%s); retrying once with correction",
                    topic.key, exc)
        fix = LLMRequest(
            purpose="research_fix", model=req.model, system=req.system,
            messages=[*req.messages,
                      {"role": "assistant", "content": resp.text[:8000] or "(no output)"},
                      {"role": "user", "content":
                       f"Your reply was not valid JSON of the required shape ({exc}). Reply again "
                       "with ONLY the JSON object {\"stories\": [...]}, using only stories you "
                       "already found. No prose, no code fences."}],
            max_tokens=req.max_tokens, effort="low", context=req.context,
        )
        cfg = pipeline_cfg().research
        fixed = with_retries(lambda: providers.llm.complete(fix), cfg.max_attempts,
                             cfg.backoff_base_sec)
        record_llm(session, "research", f"topic:{topic.key}", fixed)
        return _parse(fixed.text)  # second failure propagates (job retry / dead letter)


def _other_sources(raw: RawStory, primary: str, limit: int) -> list[dict[str, str]]:
    """Other outlets that reported the story: named, valid URL, not the primary link."""
    out: list[dict[str, str]] = []
    for item in raw.other_sources:
        name = strip_citations(str(item.get("source_name") or item.get("name") or "")).strip()
        url = str(item.get("url") or "").strip()
        if (2 <= len(name) <= 120 and re.match(r"^https?://[^\s/]+\.[^\s/]+", url)
                and canonicalize_url(url) != primary
                and all(canonicalize_url(url) != canonicalize_url(o["url"]) for o in out)):
            out.append({"name": name, "url": url})
    return out[:limit]


def _on_topic(session: Session, topic: Topic, providers: Providers,
              stories: list[RawStory]) -> set[int]:
    """Indexes of the stories that are really about the topic (one SMALL_MODEL call). The same
    call drops background terms that aren't central to their story and the topic (they would
    become off-topic "some context" segments). If the call fails, every story is kept and
    background terms are dropped: research is not lost to a classifier hiccup, and no
    unchecked context reaches an episode."""
    items = [{"id": i, "headline": s.headline, "summary": s.summary[:300],
              "background_terms": s.background_terms} for i, s in enumerate(stories)]
    req = LLMRequest(
        purpose="research_relevance", model=providers.small_model,
        system=load_prompt("relevance_system.txt"),
        messages=[{"role": "user", "content": json.dumps(
            {"topic": topic.display_name, "stories": items}, ensure_ascii=False)}],
        max_tokens=1500, effort="low", context={"topic": topic.display_name, "stories": items},
    )
    try:
        resp = providers.llm.complete(req)
        record_llm(session, "research_relevance", f"topic:{topic.key}", resp)
        data = extract_json(resp.text)
        off = {int(x) for x in data.get("off_topic", [])}
        drop = {(int(t["id"]), str(t["term"]).strip().lower())
                for t in data.get("off_topic_terms", []) if isinstance(t, dict)}
        for i, story in enumerate(stories):
            story.background_terms = [t for t in story.background_terms
                                      if (i, t.strip().lower()) not in drop]
        return {i for i in range(len(stories)) if i not in off}
    except (LLMError, ValueError, TypeError, AttributeError, KeyError) as exc:
        log.warning("Relevance check failed for %s (%s); keeping all stories", topic.key, exc)
        for story in stories:
            story.background_terms = []
        return set(range(len(stories)))


def prune_background(session: Session, topic: Topic, providers: Providers,
                     stories: list[Story]) -> int:
    """Re-run the background-term check on stored stories (e.g. after the rule changed):
    drops the background entries that aren't central to their story and the topic. Their
    explainers stay stored but are never read, since explainers only follow their story."""
    from types import SimpleNamespace

    items = [SimpleNamespace(headline=s.headline, summary=s.summary,
                             background_terms=[b["term"] for b in s.background or []])
             for s in stories]
    if not items:
        return 0
    _on_topic(session, topic, providers, items)  # type: ignore[arg-type]
    dropped = 0
    for story, item in zip(stories, items, strict=True):
        keep = [b for b in story.background or [] if b["term"] in item.background_terms]
        dropped += len(story.background or []) - len(keep)
        story.background = keep
        session.add(story)
    session.commit()
    return dropped


def _background(raw: RawStory, language: str, limit: int) -> list[dict[str, str]]:
    """Wikipedia context for the terms the researcher flagged (free; may be empty)."""
    found = []
    for term in raw.background_terms[:limit]:
        if isinstance(term, str) and (b := web.wikipedia_background(term, language)):
            found.append(b)
    return found


def ingest(session: Session, topic: Topic, providers: Providers, req: LLMRequest,
           resp: LLMResponse, lookback_hours: int, now: datetime,
           language: str = "en") -> ResearchResult:
    """Validate, filter, dedupe and insert the stories from one research response."""
    subtopics = list(session.exec(select(Subtopic).where(Subtopic.topic_id == topic.id)))
    cfg = pipeline_cfg().research
    record_llm(session, "research", f"topic:{topic.key}", resp)
    session.commit()
    result = ResearchResult(topic.key, language)
    items = _parse_with_fix(session, topic, providers, req, resp)
    result.returned = len(items)

    valid: list[tuple[RawStory, datetime, str]] = []
    seen_urls: set[str] = set()
    for item in items:
        try:
            raw = RawStory.model_validate(item)
            published, date_only = parse_published(raw.published_date)
        except (ValidationError, ValueError) as exc:
            result.invalid += 1
            log.debug("Dropping invalid story for %s: %s", topic.key, exc)
            continue
        if not in_window(published, date_only, now, lookback_hours):
            result.out_of_window += 1
            continue
        canon = canonicalize_url(raw.url)
        if canon in seen_urls:
            result.duplicates += 1
            continue
        seen_urls.add(canon)
        valid.append((raw, min(published, now), canon))

    # the same event is known if its link, or any outlet listed as also reporting it, is the
    # link of a recent story (or one of that story's other sources)
    since = now - timedelta(days=cfg.dedupe_window_days)
    known: set[str] = set()
    for url, sources in session.exec(select(Story.canonical_url, Story.sources).where(
            col(Story.first_seen) >= since)):
        known.add(url)
        known.update(canonicalize_url(s["url"]) for s in (sources or []) if s.get("url"))
    known |= set(session.exec(select(Story.canonical_url).where(
        col(Story.canonical_url).in_(list(seen_urls))))) if seen_urls else set()

    def is_known(raw: RawStory, canon: str) -> bool:
        others = {canonicalize_url(str(o.get("url", ""))) for o in raw.other_sources
                  if isinstance(o, dict) and o.get("url")}
        return canon in known or bool(others & known)
    fresh = [v for v in valid if not is_known(v[0], v[2])]
    result.duplicates += len(valid) - len(fresh)

    if cfg.relevance_check and fresh:  # one small call: drop stories not about the topic
        keep = _on_topic(session, topic, providers, [raw for raw, _, _ in fresh])
        result.off_topic = len(fresh) - len(keep)
        fresh = [v for i, v in enumerate(fresh) if i in keep]

    per_source: dict[str, int] = {}  # plural sourcing: cap stories per outlet, best first
    capped = []
    for v in sorted(fresh, key=lambda v: -v[0].importance):
        outlet = re.sub(r"[^a-z0-9]", "", v[0].source_name.lower())
        per_source[outlet] = per_source.get(outlet, 0) + 1
        if per_source[outlet] > cfg.max_per_source:
            result.over_source_cap += 1
        else:
            capped.append(v)
    fresh = [v for v in fresh if v in capped]

    if cfg.verify_urls and fresh:  # drop section/tag pages and links that don't exist
        with ThreadPoolExecutor(8) as pool:
            alive = list(pool.map(web.article_link_ok, [raw.url for raw, _, _ in fresh]))
        result.dead_links = alive.count(False)
        fresh = [v for v, ok in zip(fresh, alive, strict=True) if ok]

    vectors = providers.embedder.embed([raw.headline for raw, _, _ in fresh])
    accepted: list[tuple[list[float], datetime]] = []
    for (raw, published, canon), vec in zip(fresh, vectors, strict=True):
        if any(sum(a * b for a, b in zip(vec, v, strict=True)) >= cfg.headline_dup_threshold
               for v, _ in accepted):
            result.duplicates += 1
            continue
        update_of: int | None = None
        nearest = providers.vectors.nearest_stories(session, vec, 1, topic.id, since)
        if nearest and nearest[0][1] >= cfg.headline_dup_threshold:
            old = nearest[0][0]
            if published - as_utc(old.published_at) >= timedelta(hours=cfg.update_min_hours):
                update_of = old.id  # same event, materially newer report: keep as an update
            else:
                result.duplicates += 1
                continue
        story = Story(
            topic_id=topic.id,  # type: ignore[arg-type]
            canonical_url=canon,
            headline=raw.headline.strip(),
            summary=raw.summary.strip(),
            why_it_matters=raw.why_it_matters.strip(),
            source_name=raw.source_name.strip(),
            published_at=published,
            importance_score=raw.importance,
            embedding=vec,
            first_seen=now,
            status="update" if update_of else "active",
            update_of_id=update_of,
            language=language,
            sources=_other_sources(raw, canon, cfg.max_other_sources),
            background=_background(raw, language, cfg.background_terms),
        )
        tag_stories([story], subtopics)  # classify into the topic's subcategories (free)
        session.add(story)
        session.flush()
        ensure_explainers(session, topic, story, providers, now)
        accepted.append((vec, published))
        result.new_story_ids.append(story.id)  # type: ignore[arg-type]
        result.updates += bool(update_of)
    session.commit()
    log.info("Research %s/%s: %d returned, %d new, %d dup, %d invalid, %d out of window, "
             "%d off topic, %d over the per-outlet cap, %d bad links", topic.key, language,
             result.returned, len(result.new_story_ids), result.duplicates, result.invalid,
             result.out_of_window, result.off_topic, result.over_source_cap, result.dead_links)
    return result


def ensure_explainers(session: Session, topic: Topic, story: Story, providers: Providers,
                      now: datetime) -> list[Story]:
    """One explainer per Wikipedia background of a new story: a Story with status
    "explainer" (source Wikipedia), used only to fill short episodes (step 4). Shared like any
    story: one per Wikipedia article, scripted and recorded only when an episode uses it. An
    article mentioned again is refreshed so it can fill this week's episodes."""
    made = []
    for b in story.background or []:
        if not b.get("url") or not b.get("summary"):
            continue
        url = canonicalize_url(b["url"])
        explainer = session.exec(select(Story).where(Story.canonical_url == url)).first()
        if explainer is None:
            explainer = Story(
                topic_id=topic.id, canonical_url=url,  # type: ignore[arg-type]
                headline=b.get("title") or b["term"], summary=b["summary"],
                source_name="Wikipedia", importance_score=1, status="explainer",
                language=b.get("language") or story.language, published_at=now,
                first_seen=now, embedding=providers.embedder.embed_one(b.get("title") or b["term"]))
        elif explainer.status == "explainer":
            explainer.published_at = explainer.first_seen = now
        else:
            continue
        session.add(explainer)
        made.append(explainer)
    session.flush()
    return made


def research_topic(session: Session, topic: Topic, providers: Providers,
                   lookback_hours: int | None = None, now: datetime | None = None,
                   force: bool = False, language: str = "en") -> ResearchResult:
    """Research one topic in one language (only if it has active subscribers, unless forced)."""
    cfg = pipeline_cfg().research
    lookback_hours = lookback_hours or cfg.lookback_hours
    now = now or utcnow()
    if topic.active_subscribers <= 0 and not force:
        log.info("Skipping research for %s: no active subscribers", topic.key)
        return ResearchResult(topic.key, language, skipped=True)
    req = build_request(session, topic, providers, lookback_hours, now, language)
    resp = with_retries(lambda: providers.llm.complete(req), cfg.max_attempts,
                        cfg.backoff_base_sec)
    return ingest(session, topic, providers, req, resp, lookback_hours, now, language)


def research_many(
    session: Session, units: list[tuple[Topic, str]], providers: Providers, batch: bool,
    now: datetime | None = None,
) -> dict[tuple[str, str], ResearchResult | Exception]:
    """Research several (topic, language) units, optionally through the batch API (50% off)."""
    cfg = pipeline_cfg().research
    now = now or utcnow()
    reqs = [build_request(session, t, providers, cfg.lookback_hours, now, lang)
            for t, lang in units]
    responses = providers.llm.complete_many(reqs, batch=batch)
    out: dict[tuple[str, str], ResearchResult | Exception] = {}
    for (topic, lang), req, resp in zip(units, reqs, responses, strict=True):
        try:
            if isinstance(resp, TransientLLMError):
                resp = with_retries(lambda r=req: providers.llm.complete(r), cfg.max_attempts,
                                    cfg.backoff_base_sec)
            if isinstance(resp, Exception):
                raise resp
            out[(topic.key, lang)] = ingest(session, topic, providers, req, resp,
                                            cfg.lookback_hours, now, lang)
        except Exception as exc:  # noqa: BLE001 - reported per unit
            session.rollback()
            out[(topic.key, lang)] = exc
    return out
