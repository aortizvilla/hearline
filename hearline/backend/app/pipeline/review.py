"""Accuracy review: the whole episode is checked before it is recorded (between steps 5 and 6).

Every script was already fact-checked on its own in step 3. This step looks at the episode as
the listener will hear it, which no single-script check can:

  1. free checks: a sentence that repeats one from an earlier segment (word overlap)
  2. one SMALL_MODEL review (prompts/review_system.txt) of every segment against its story
     record: the same news told twice, a claim not in the record (teasers included: they are
     written by step 5 and never fact-checked otherwise), background that doesn't explain the
     story before it, contradictions between segments

Flagged segments are removed and the episode is re-planned without them (step 4), at most
`assemble.review.rounds` times. A flagged teaser is dropped from the rundown and its cached text
deleted, so the next listener gets a new one. Results are cached by content
(dim_episode_review): listeners whose episodes read the same segments share one review, and
each episode stores what was found and removed (fact_episode.quality).

If the review call fails the job is retried (TransientLLMError): an unreviewed episode is never
recorded.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlmodel import Session, col, select

from app.config import language_names, load_prompt, pipeline_cfg
from app.database.models import EpisodeReview, PhraseClip
from app.pipeline.step3_scripts import story_record
from app.providers import Providers
from app.providers.llm import LLMError, LLMRequest, TransientLLMError, extract_json
from app.tracking.costs import record_llm

if TYPE_CHECKING:
    from app.pipeline.step4_assemble import EpisodePlan

log = logging.getLogger(__name__)

ISSUE_TYPES = {"redundant", "unsupported", "off_topic", "incoherent"}
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"segment": {"type": "string"},
                               "type": {"type": "string", "enum": sorted(ISSUE_TYPES)},
                               "detail": {"type": "string"}},
                "required": ["segment", "type", "detail"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["issues"],
    "additionalProperties": False,
}


@dataclass
class ReviewResult:
    issues: list[dict[str, str]] = field(default_factory=list)
    cached: bool = False

    def story_ids(self) -> set[int]:
        """Stories (news or background) to take out of the episode."""
        return {int(i["segment"].split(":")[1]) for i in self.issues
                if i["segment"].startswith(("story:", "background:"))}

    def teaser_ids(self) -> set[int]:
        return {int(i["segment"].split(":")[1]) for i in self.issues
                if i["segment"].startswith("teaser:")}


def segments(plan: EpisodePlan) -> list[dict[str, Any]]:
    """The episode as it will be heard: teasers, then each story with its background."""
    out: list[dict[str, Any]] = []
    teasers = plan.frame.get("teaser_ids", [])
    by_id = {c.story.id: (c, clip) for c, clip in plan.items}
    for sid, text in zip(teasers, plan.frame.get("rundown", [])[1:], strict=False):
        if sid in by_id:
            out.append({"id": f"teaser:{sid}", "kind": "teaser", "text": text,
                        "record": story_record(by_id[sid][0].story)})
    last_headline = ""
    for c, clip in plan.items:
        if c.story.status == "explainer":
            out.append({"id": f"background:{c.story.id}", "kind": "background",
                        "text": clip.script, "record": story_record(c.story),
                        "explains_story": last_headline})
        else:
            last_headline = c.story.headline
            out.append({"id": f"story:{c.story.id}", "kind": "story", "text": clip.script,
                        "record": story_record(c.story)})
    return out


def _sentences(text: str) -> list[set[str]]:
    words = [set(re.findall(r"\w{3,}", s.lower())) for s in re.split(r"(?<=[.!?])\s+", text)]
    return [w for w in words if len(w) >= 8]


def repeated_sentences(segs: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Free check: a story/background sentence that mostly repeats one of an earlier segment."""
    share = pipeline_cfg().assemble.review.redundant_sentence_share
    seen: list[tuple[str, set[str]]] = []
    issues = []
    for seg in segs:
        if seg["kind"] == "teaser":
            continue
        mine = _sentences(seg["text"])
        for words in mine:
            for other_id, other in seen:
                if len(words & other) >= share * min(len(words), len(other)):
                    issues.append({"segment": seg["id"], "type": "redundant",
                                   "detail": f"repeats a sentence from {other_id}"})
                    break
            else:
                continue
            break
        seen += [(seg["id"], w) for w in mine]
    return issues


def review_key(segs: list[dict[str, Any]], language: str) -> str:
    content = json.dumps([language, [(s["id"], s["text"]) for s in segs]], ensure_ascii=False)
    return hashlib.sha256(content.encode()).hexdigest()


def review_request(segs: list[dict[str, Any]], language: str,
                   providers: Providers) -> LLMRequest:
    cfg = pipeline_cfg().assemble.review
    payload = {"language": language_names().get(language, language), "segments": segs}
    return LLMRequest(
        purpose="episode_review", model=providers.small_model,
        system=load_prompt("review_system.txt"),
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False,
                                                         indent=1)}],
        max_tokens=cfg.max_tokens, effort=cfg.effort, json_schema=REVIEW_SCHEMA,
        context={"segments": segs, "language": language},
    )


def review_plan(session: Session, providers: Providers, plan: EpisodePlan,
                ref: str) -> ReviewResult:
    """Review the planned episode (its frame must be written: frame_for). Cached by content."""
    segs = segments(plan)
    found = repeated_sentences(segs)
    if not pipeline_cfg().assemble.review.enabled or not segs:
        return ReviewResult(found)
    key = review_key(segs, plan.language)
    cached = session.exec(select(EpisodeReview).where(EpisodeReview.key == key)).first()
    if cached is not None:
        return ReviewResult(_merge(found, cached.issues), cached=True)
    try:
        resp = providers.llm.complete(review_request(segs, plan.language, providers))
        record_llm(session, "episode_review", ref, resp)
        raw = extract_json(resp.text).get("issues", [])
    except (LLMError, ValueError, AttributeError) as exc:
        raise TransientLLMError(
            f"accuracy review failed ({exc}); not recording unreviewed") from exc
    ids = {s["id"] for s in segs}
    issues = [{"segment": str(i.get("segment")), "type": str(i.get("type")),
               "detail": str(i.get("detail", ""))[:300]}
              for i in raw if isinstance(i, dict) and i.get("segment") in ids
              and i.get("type") in ISSUE_TYPES
              and not (i.get("type") == "redundant" and str(i["segment"]).startswith("teaser:"))]
    session.add(EpisodeReview(key=key, language=plan.language, issues=issues,
                              model=providers.small_model))
    session.commit()
    return ReviewResult(_merge(found, issues))


def _merge(a: list[dict[str, str]], b: list[dict[str, str]]) -> list[dict[str, str]]:
    seen = {(i["segment"], i["type"]) for i in a}
    return a + [i for i in b if (i["segment"], i["type"]) not in seen]


def forget_teasers(session: Session, story_ids: set[int]) -> None:
    """A flagged teaser's cached text and recordings go, so it is written anew next time."""
    keys = [f"teaser:story:{sid}" for sid in story_ids]
    for row in session.exec(select(PhraseClip).where(col(PhraseClip.key).in_(keys))):
        session.delete(row)
    session.commit()


def audit_episode(session: Session, episode: Any) -> list[str]:
    """Re-check a stored episode against every format and accuracy rule (no model call):
    the step 3 script checks on each segment, one subject per episode, background placement,
    and what its accuracy review found. Used by `python -m app.cli check-episodes`."""
    from app.pipeline.step3_scripts import local_issues
    from app.pipeline.step4_assemble import episode_items, same_subject

    problems: list[str] = []
    items = episode_items(session, episode)
    news = [st for _, st, _ in items if st.status != "explainer"]
    for i, (_, story, clip) in enumerate(items):
        for issue in local_issues(clip.script, story, episode.language):
            if not issue.startswith("length"):  # length is checked when the script is written
                problems.append(f"story {story.id}: {issue}")
        if not clip.verified:
            problems.append(f"story {story.id}: script never passed the fact-check")
        if story.status == "explainer":
            before = items[i - 1][1] if i else None
            if before is None or not any(b.get("url") == story.canonical_url
                                         for b in before.background or []):
                problems.append(f"background {story.id}: not right after the story it explains")
    for i, a in enumerate(news):
        problems += [f"stories {a.id} and {b.id}: same subject told twice"
                     for b in news[i + 1:] if same_subject(a, b)]
    quality = episode.quality or {}
    if not quality.get("reviewed"):
        problems.append("no accuracy review recorded")
    return problems
