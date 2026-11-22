"""Step 5: the episode's greeting, rundown, transitions and outro as shared spoken phrases.

Nothing here is unique to one listener, so each phrase is written once (one SMALL_MODEL call
for all missing phrases of an episode, prompt: prompts/phrase_system.txt) and recorded once per
voice, then reused:

    greeting:<time of day>:<first name>   "Good morning, Ana. Welcome to your Hearline briefing."
    rundown:fixed                          "Here's what's coming up."           (fixed wording)
    teaser:story:<story id>                "Nvidia stops selling H200 chips in China."
    transition:first:<topic key>           "Let's start with Technology."       (fixed wording)
    transition:next:<topic key>            "Now, on to History."                (fixed wording)
    outro:<variant>                        rotates by day

The rundown and the transitions use fixed wording per language (RUNDOWN, TRANSITIONS): they
can never name the wrong topics, the topic name is always the listener-language name, and a
topic change always sounds like one. Only teasers mention the news: one per story, shared by
every listener who gets that story, so an episode's "coming up" rundown is stitched from
shared clips with no per-user call.

A phrase recorded for one voice gives its text to the others: only the audio is redone.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.config import get_settings, language_names, load_prompt, pipeline_cfg, tones_cfg
from app.database.models import PhraseClip, Story, Topic
from app.providers import Providers
from app.providers.llm import LLMError, LLMRequest, TransientLLMError, extract_json
from app.providers.storage import get_storage
from app.providers.tts import TTSProvider, recorded_by
from app.tracking import metrics
from app.tracking.costs import record_llm, record_tts

log = logging.getLogger(__name__)

PHRASE_SCHEMA = {
    "type": "object",
    "properties": {
        "phrases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"key": {"type": "string"}, "text": {"type": "string"}},
                "required": ["key", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["phrases"],
    "additionalProperties": False,
}
MAX_PHRASE_CHARS = 300
MAX_TEASER_CHARS = 160
PLACEHOLDER_NAMES = {"listener"}  # the API's default user name: greet without a name
FIXED_KINDS = {"rundown", "transition"}  # fixed wording, no model call

RUNDOWN = {"en": "Here's what's coming up.", "es": "Esto es lo que te contamos hoy.",
           "fr": "Voici ce qui vous attend.", "de": "Das erwartet dich heute.",
           "it": "Ecco cosa ti aspetta oggi.", "pt": "Veja o que vem a seguir."}
TRANSITIONS = {  # (first topic, every later topic)
    "en": ("Let's start with {t}.", "Now, on to {t}."),
    "es": ("Empezamos con {t}.", "Pasamos ahora a {t}."),
    "fr": ("On commence avec le thème {t}.", "Passons maintenant au thème {t}."),
    "de": ("Wir beginnen mit dem Thema {t}.", "Weiter geht es mit dem Thema {t}."),
    "it": ("Cominciamo con il tema {t}.", "Passiamo ora al tema {t}."),
    "pt": ("Começamos com o tema {t}.", "Agora, passamos ao tema {t}."),
}


@dataclass
class PhraseSpec:
    """One phrase an episode needs, with what the writer must know to write it."""

    key: str
    kind: str  # greeting | rundown | teaser | transition | outro
    name: str = ""
    daypart: str = ""
    topic: str = ""  # transition: the topic's name in the episode's language
    first: bool = False  # transition: opens the first topic
    variant: int = 0
    headline: str = ""  # teaser only: the story it previews
    summary: str = ""

    def brief(self) -> dict[str, str | int]:
        """What the writer gets for this phrase."""
        if self.kind == "greeting":
            return {"key": self.key, "kind": self.kind, "time_of_day": self.daypart,
                    "name": self.name or "(no name: greet without one)"}
        if self.kind == "transition":
            return {"key": self.key, "kind": self.kind, "topic": self.topic}
        if self.kind == "teaser":
            return {"key": self.key, "kind": self.kind, "headline": self.headline,
                    "summary": self.summary}
        if self.kind == "rundown":
            return {"key": self.key, "kind": self.kind}
        return {"key": self.key, "kind": self.kind, "variant": self.variant}


def daypart(hour: int) -> str:
    return "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"


def clean_name(full_name: str) -> str:
    """First name if it's safe to say aloud and cache by, else '' (greet without a name)."""
    first = (full_name or "").strip().split(" ")[0]
    if first.lower() in PLACEHOLDER_NAMES:
        return ""
    return first.capitalize() if re.fullmatch(r"[^\W\d_]{1,20}", first) else ""


def topic_label(topic: Topic, language: str) -> str:
    """The topic's name in this language (dim_topic.labels), else its English name."""
    return (topic.labels or {}).get(language) or (topic.labels or {}).get("en") \
        or topic.display_name


def episode_phrases(first_name: str, hour: int, topics: list[Topic], day: date,
                    language: str = "en") -> tuple[PhraseSpec, list[PhraseSpec], PhraseSpec]:
    """The greeting, one transition per topic (in order: the first one opens, the others
    move on) and today's outro variant."""
    part, name = daypart(hour), clean_name(first_name)
    greeting = PhraseSpec(f"greeting:{part}:{name.lower()}", "greeting", name=name, daypart=part)
    transitions = [PhraseSpec(f"transition:{'first' if i == 0 else 'next'}:{t.key}",
                              "transition", topic=topic_label(t, language), first=i == 0)
                   for i, t in enumerate(topics)]
    variant = day.toordinal() % max(1, pipeline_cfg().assemble.outro_variants) + 1
    return greeting, transitions, PhraseSpec(f"outro:{variant}", "outro", variant=variant)


def rundown_phrases(stories: list[Story]) -> list[PhraseSpec]:
    """The "coming up" line and one teaser per story (best first, up to `assemble.teasers`).
    Empty for a one-story episode: a rundown of one story is just the story."""
    n = pipeline_cfg().assemble.teasers
    if n <= 0 or len(stories) < 2:
        return []
    return [PhraseSpec("rundown:fixed", "rundown")] + [
        PhraseSpec(f"teaser:story:{s.id}", "teaser", headline=s.headline, summary=s.summary)
        for s in stories[:n]]


def fixed_text(spec: PhraseSpec, language: str) -> str:
    """The fixed wording of a rundown or transition line in this language."""
    if spec.kind == "rundown":
        return RUNDOWN.get(language, RUNDOWN["en"])
    first, nxt = TRANSITIONS.get(language, TRANSITIONS["en"])
    return (first if spec.first else nxt).format(t=spec.topic)


def end_sentence(text: str) -> str:
    """Spoken lines end with punctuation, so the voice closes the sentence."""
    text = text.strip()
    return text if not text or text[-1] in ".!?…»\"'" else text + "."


def english_template(spec: PhraseSpec) -> str:
    """Fallback text if the writer call fails (English only; other languages retry later)."""
    if spec.kind == "greeting":
        who = f", {spec.name}" if spec.name else ""
        return f"Good {spec.daypart}{who}. Welcome to your Hearline briefing."
    if spec.kind == "transition":
        return f"Now, {spec.topic}."
    if spec.kind == "rundown":
        return "Here's what's coming up."
    if spec.kind == "teaser":
        return spec.headline.rstrip(".") + "."
    return ["That's your Hearline briefing for today. Thanks for listening.",
            "And that's the news for today. Talk to you tomorrow.",
            "That's all for now. See you tomorrow on Hearline."][(spec.variant - 1) % 3]


def phrase_issues(spec: PhraseSpec, text: str) -> list[str]:
    issues = []
    if not text or len(text) > MAX_PHRASE_CHARS:
        issues.append(f"length {len(text)} chars")
    if re.search(r"https?://|www\.|[*#_`\[\]]", text):
        issues.append("URL or markdown")
    if spec.kind == "greeting" and spec.name and spec.name.lower() not in text.lower():
        issues.append("greeting does not use the name")
    if spec.kind == "teaser" and len(text) > MAX_TEASER_CHARS:
        issues.append(f"teaser {len(text)} chars")
    return issues


def _write(session: Session, providers: Providers, specs: list[PhraseSpec], tone: str,
           language: str, ref: str) -> dict[str, str]:
    """One small-model call for every phrase that has no text yet (fixed-wording phrases
    need none)."""
    texts: dict[str, str] = {s.key: fixed_text(s, language) for s in specs
                             if s.kind in FIXED_KINDS}
    specs = [s for s in specs if s.kind not in FIXED_KINDS]
    if not specs:
        return texts
    cfg = pipeline_cfg().assemble.phrase_llm
    preset = tones_cfg().presets[tone]
    content = {"language": language_names().get(language, language),
               "tone": f"{preset.label}. {preset.instructions}",
               "phrases": [s.brief() for s in specs]}
    req = LLMRequest(
        purpose="phrase_write", model=providers.small_model,
        system=load_prompt("phrase_system.txt"),
        messages=[{"role": "user", "content": json.dumps(content, ensure_ascii=False, indent=1)}],
        max_tokens=cfg.max_tokens,
        effort=cfg.effort, json_schema=PHRASE_SCHEMA,
        context={"phrases": [s.brief() for s in specs], "language": language},
    )
    try:
        resp = providers.llm.complete(req)
        record_llm(session, "phrase_write", ref, resp)
        written = {str(p.get("key")): end_sentence(str(p.get("text", "")))
                   for p in extract_json(resp.text).get("phrases", []) if isinstance(p, dict)}
    except (LLMError, ValueError, AttributeError) as exc:
        log.warning("Phrase writing failed (%s)", exc)
        written = {}
    for spec in specs:
        text = written.get(spec.key, "")
        if not phrase_issues(spec, text):
            texts[spec.key] = text
        elif language == "en":
            texts[spec.key] = english_template(spec)
        else:  # no safe text in this language: let the job retry later
            raise TransientLLMError(f"could not write phrase {spec.key} in {language}")
    return texts


def _audio_name(phrase: PhraseClip, tts: TTSProvider) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", phrase.key.lower()).strip("_")
    tag = tts.voice_tag(phrase.voice, phrase.language)
    return f"phrases/{tag}/{phrase.tone}_{phrase.language}_{phrase.voice}/{slug}"


def ensure_phrases(session: Session, providers: Providers, specs: list[PhraseSpec], tone: str,
                   language: str, voice: str, ref: str, synthesize: bool = True
                   ) -> dict[str, PhraseClip]:
    """Phrases for one (tone, language, voice): cached ones are reused, missing text is
    written once, missing audio is recorded once."""
    keys = list(dict.fromkeys(s.key for s in specs))
    rows = session.exec(select(PhraseClip).where(
        col(PhraseClip.key).in_(keys), PhraseClip.tone == tone,
        PhraseClip.language == language)).all()
    mine = {p.key: p for p in rows if p.voice == voice}
    other_voice = {p.key: p.text for p in rows if p.voice != voice}
    metrics.incr("phrase_cache_hit", len(mine))

    missing = [s for s in {s.key: s for s in specs}.values() if s.key not in mine]
    reused = {s.key: other_voice[s.key] for s in missing if s.key in other_voice}
    metrics.incr("phrase_text_reused", len(reused))
    to_write = [s for s in missing if s.key not in reused]
    texts = {**reused, **(_write(session, providers, to_write, tone, language, ref)
                          if to_write else {})}

    for key, text in texts.items():
        phrase = PhraseClip(key=key, tone=tone, language=language, voice=voice, text=text)
        try:
            with session.begin_nested():
                session.add(phrase)
        except IntegrityError:  # another worker created it meanwhile
            phrase = session.exec(select(PhraseClip).where(
                PhraseClip.key == key, PhraseClip.tone == tone,
                PhraseClip.language == language, PhraseClip.voice == voice)).one()
        mine[key] = phrase

    if synthesize:
        audio_dir = get_settings().audio_dir
        for phrase in mine.values():
            if not recorded_by(phrase.audio_path, providers.tts, voice, language):
                res = providers.tts.synthesize(phrase.text,
                                               audio_dir / _audio_name(phrase, providers.tts),
                                               language, voice)
                phrase.audio_path = str(res.path.relative_to(audio_dir))
                get_storage().save(phrase.audio_path)
                phrase.duration_sec = round(res.duration_sec, 2)
                record_tts(session, "phrase_tts", f"phrase:{phrase.key}", providers.tts.name,
                           res.chars)
                session.add(phrase)
    session.commit()
    return mine
