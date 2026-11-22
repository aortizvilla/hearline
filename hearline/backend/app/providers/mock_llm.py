"""Deterministic offline LLM used by tests and `--mock` runs.

It answers each pipeline purpose with text in the same format a real model is asked for, so
the real parsing/validation/costing code runs. Token usage is synthetic (chars/4, plus a fixed
allowance for web-search result tokens) so offline cost estimates are realistic.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timedelta
from typing import Any

from app.providers.llm import LLMProvider, LLMRequest, LLMResponse, Usage

SOURCES = [
    ("Reuters", "reuters.com"),
    ("Associated Press", "apnews.com"),
    ("BBC News", "bbc.co.uk"),
    ("The Guardian", "theguardian.com"),
    ("Bloomberg", "bloomberg.com"),
    ("NPR", "npr.org"),
]
ACTORS = ["Regulators", "A coalition of researchers", "The largest industry group",
          "City officials", "A new startup", "Two rival companies", "An international panel",
          "Lawmakers",
          "A university team", "Investors", "A government agency", "Independent auditors"]
ACTIONS = ["unveil", "approve", "delay", "investigate", "launch", "criticize", "expand",
           "pause", "fund", "publish", "settle", "test"]
OBJECTS = ["a landmark standard", "an ambitious pilot program", "a long-awaited report",
           "new safety rules", "a record funding round", "a cross-border agreement",
           "a controversial merger", "an open dataset", "a major recall", "a public consultation",
           "a prototype", "an emergency plan"]
MIN_CACHEABLE_TOKENS = 1024


def _rng(seed: str) -> int:
    return int(hashlib.sha256(seed.encode()).hexdigest()[:12], 16)


class MockLLM(LLMProvider):
    name = "mock"
    supports_batches = True

    def __init__(
        self,
        stories_per_topic: int = 6,
        research_search_tokens: int = 18000,
        searches_per_research: int = 3,
        fail_verify_story_ids: set[int] | None = None,
        invalid_json_first: set[str] | None = None,
    ) -> None:
        self.stories_per_topic = stories_per_topic
        self.research_search_tokens = research_search_tokens
        self.searches_per_research = searches_per_research
        self.fail_verify_story_ids = fail_verify_story_ids or set()
        self.invalid_json_first = invalid_json_first or set()  # purposes to break once
        self.calls: list[LLMRequest] = []
        self._cached_prefixes: set[str] = set()
        self._lock = threading.Lock()

    # -- public ---------------------------------------------------------------
    def complete(self, req: LLMRequest) -> LLMResponse:
        with self._lock:
            self.calls.append(req)
            broken = req.purpose in self.invalid_json_first
            self.invalid_json_first.discard(req.purpose)
        handler = getattr(self, f"_{req.purpose}")
        text = "Sure! Here are the results: {not json" if broken else handler(req)
        return LLMResponse(text=text, model=req.model, usage=self._usage(req, text),
                           provider=self.name)

    def complete_many(
        self, reqs: list[LLMRequest], batch: bool = False
    ) -> list[LLMResponse | Exception]:
        out = super().complete_many(reqs)
        for r in out:
            if isinstance(r, LLMResponse):
                r.batched = batch and len(reqs) > 1
        return out

    def count(self, purpose: str) -> int:
        return sum(1 for c in self.calls if c.purpose == purpose)

    # -- usage ----------------------------------------------------------------
    def _usage(self, req: LLMRequest, text: str) -> Usage:
        sys_tokens = len(req.system) // 4
        msg_tokens = len(json.dumps(req.messages, default=str)) // 4
        u = Usage(input_tokens=msg_tokens, output_tokens=max(1, len(text) // 4))
        if sys_tokens >= MIN_CACHEABLE_TOKENS:
            key = f"{req.model}:{hashlib.md5(req.system.encode()).hexdigest()}"
            with self._lock:
                hit = key in self._cached_prefixes
                self._cached_prefixes.add(key)
            if hit:
                u.cache_read_tokens = sys_tokens
            else:
                u.cache_write_tokens = sys_tokens
        else:
            u.input_tokens += sys_tokens
        if req.purpose == "research":
            u.input_tokens += self.research_search_tokens
            u.searches = self.searches_per_research
        return u

    # -- purposes -------------------------------------------------------------
    def _research(self, req: LLMRequest) -> str:
        ctx = req.context
        key, name = ctx["topic_key"], ctx["display_name"]
        now = datetime.fromisoformat(ctx["now"])
        keywords: list[str] = ctx.get("keywords", [])
        day = now.date().isoformat()
        stories: list[dict[str, Any]] = []
        for i in range(self.stories_per_topic):
            r = _rng(f"{key}|{day}|{i}")
            actor = ACTORS[r % len(ACTORS)]
            action = ACTIONS[(r // 7) % len(ACTIONS)]
            obj = OBJECTS[(r // 49) % len(OBJECTS)]
            source, domain = SOURCES[(r // 343) % len(SOURCES)]
            focus = keywords[i % len(keywords)] if keywords and i % 2 == 0 else name.lower()
            headline = f"{actor} {action} {obj} in {focus}"
            slug = re.sub(r"[^a-z0-9]+", "-", headline.lower()).strip("-")
            published = now - timedelta(hours=1 + i * 3)
            stories.append({
                "headline": headline,
                "summary": (f"{actor} moved to {action} {obj} affecting {focus}, according to "
                            f"{source}. The decision was announced on {published:%B %d}. "
                            f"Officials said details would follow in the coming weeks."),
                "why_it_matters": f"It could change how {focus} develops over the next year.",
                "source_name": source,
                "url": f"https://www.{domain}/{key}/{day}/{slug}?utm_source=feed&ref=home#top",
                "published_date": published.isoformat(),
                "importance": 3 + r % 8,
            })
        stories.append({"headline": f"Rumor without a source about {name}", "summary": "x",
                        "why_it_matters": "", "source_name": "", "url": "",
                        "published_date": now.isoformat(), "importance": 2})
        return json.dumps({"stories": stories})

    def _research_fix(self, req: LLMRequest) -> str:
        return self._research(req)

    def _clip_write(self, req: LLMRequest) -> str:
        s = req.context["story"]
        lead = {"formal": "reports that", "energetic": "says", "calm": "reports that"}.get(
            req.context.get("tone", ""), "reports that")
        if req.context.get("language", "en") != "en":
            lead = "·"  # a real model writes the attribution in the requested language
        headline = s["headline"][0].lower() + s["headline"][1:]
        background = "source_name" not in s  # a background explainer names no source
        curiosity = " ".join(b["context"] for b in s.get("background", []))[:200]
        text = f"{s.get('source_name')} {lead} {headline}. {s['summary']} {s['why_it_matters']}"
        if curiosity and not background:
            text += f" {curiosity}"
        if background:
            text = f"{'Some context' if req.context.get('language', 'en') == 'en' else '·'}: " \
                   f"{headline}. {s['summary']}"
        lo, hi = req.context.get("words", [0, 10_000])
        while len(text.split()) < lo:  # respect the requested length with a factual recap
            text += (f" Once more, {headline}." if background
                     else f" Once more, {s['source_name']} {lead} {headline}.")
        if req.context.get("language", "en") != "en":  # no English phrasing in other languages
            text = re.sub(r"\b(reports that|according to|says)\b", "·", text, flags=re.I)
        return " ".join(text.split()[:hi])

    def _clip_adapt(self, req: LLMRequest) -> str:
        script = str(req.context["script"])  # same facts, same words
        if req.context.get("language", "en") != "en":  # "translated": no English phrasing left
            script = re.sub(r"\b(reports that|according to|says)\b", "·", script, flags=re.I)
        return script

    def _clip_verify_second(self, req: LLMRequest) -> str:
        return self._clip_verify(req)

    def _clip_verify(self, req: LLMRequest) -> str:
        if req.context.get("story_id") in self.fail_verify_story_ids:
            return json.dumps({"supported": False, "issues": ["Claims a number not in source"]})
        return json.dumps({"supported": True, "issues": []})

    def _topic_resolve(self, req: LLMRequest) -> str:
        text: str = req.context["text"]
        cands: list[dict[str, Any]] = req.context.get("candidates", [])
        if cands and cands[0]["similarity"] >= 0.55:
            return json.dumps({"same_as": cands[0]["key"]})
        if cands and cands[0]["similarity"] >= 0.40:
            return json.dumps({"child_of": cands[0]["key"]})
        key = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        return json.dumps({"new": {"key": key, "display_name": text.strip().title()}})

    def _research_relevance(self, req: LLMRequest) -> str:
        off = [s["id"] for s in req.context["stories"] if "off-topic" in s["headline"].lower()]
        terms = [{"id": s["id"], "term": t} for s in req.context["stories"]
                 for t in s.get("background_terms", []) if "incidental" in t.lower()]
        return json.dumps({"off_topic": off, "off_topic_terms": terms})

    def _subtopic_resolve(self, req: LLMRequest) -> str:
        phrase = str(req.context["phrase"])
        if "unrelated" in phrase.lower():
            return json.dumps({"unrelated": True})
        for s in req.context.get("existing_subcategories", []):
            if s["label"].lower() in phrase.lower():
                return json.dumps({"same_as": s["key"]})
        return json.dumps({"new": {"label": phrase.title(), "keywords": [phrase.lower()]}})

    def _episode_review(self, req: LLMRequest) -> str:
        """Flags a segment whose text says FAKE (unsupported) or REPEAT (redundant)."""
        issues = [{"segment": seg["id"], "type": kind, "detail": f"marked {word}"}
                  for seg in req.context["segments"]
                  for word, kind in (("FAKE", "unsupported"), ("REPEAT", "redundant"))
                  if word in seg["text"]]
        return json.dumps({"issues": issues})

    def _phrase_write(self, req: LLMRequest) -> str:
        out = []
        for p in req.context["phrases"]:
            if p["kind"] == "greeting":
                name = p["name"] if "(no name" not in p["name"] else ""
                text = f"Good {p['time_of_day']}{', ' + name if name else ''}. This is Hearline."
            elif p["kind"] == "transition":
                text = f"Now, {p['topic']}."
            elif p["kind"] == "rundown":
                text = "Here's what's coming up."
            elif p["kind"] == "teaser":
                text = str(p["headline"])[:100].rstrip(".") + "."
            else:
                text = f"That's your briefing. Talk tomorrow (variant {p['variant']})."
            out.append({"key": p["key"], "text": text})
        return json.dumps({"phrases": out})
