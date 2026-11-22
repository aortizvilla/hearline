"""Step 3: the spoken script for each story, written once and reused by every listener.

Cheapest path (clips.strategy: base_adapt, the default):
1. Base: the story's first script is written from the story record (WRITER_MODEL, prompt:
   prompts/script_system.txt). Conversational English is preferred when it is needed.
2. Adapt: every other (tone, language) is rewritten from a fact-checked script of the same
   story by SMALL_MODEL (prompt: prompts/adapt_system.txt). Any verified script of the story,
   from today or earlier, can be the base, so a story costs one full write ever.
Each script, base or adapted: local checks (length, no URLs or markdown, source named) ->
verify every claim against the story record (SMALL_MODEL, prompts/verify_system.txt) -> on
failure rewrite once -> verify again -> TTS only for verified scripts. If the base fails,
the other versions are written from the record instead (clips.strategy: per_combo always does).

The words are produced once per (story, tone, language), shared by every voice; audio once per
voice. Both live in one StoryClip row. Failed scripts are stored as verified=false (so they
are not retried forever) and never used in episodes. The two strategies were compared on real
stories (app/pipeline/README.md).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.config import get_settings, language_names, load_prompt, pipeline_cfg, tones_cfg
from app.database.models import Story, StoryClip
from app.providers import Providers
from app.providers.llm import LLMRequest, LLMResponse, TransientLLMError, extract_json
from app.providers.storage import get_storage
from app.providers.tts import recorded_by
from app.tracking import metrics
from app.tracking.costs import record_llm, record_tts

log = logging.getLogger(__name__)
BASE = (tones_cfg().default, "en")  # preferred base script: written from the story record

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {"type": "array", "items": {
            "type": "object",
            "properties": {"claim": {"type": "string"}, "evidence": {"type": "string"},
                           "field": {"type": "string"}, "attributed": {"type": "boolean"},
                           "supported": {"type": "boolean"}},
            "required": ["claim", "evidence", "field", "attributed", "supported"],
            "additionalProperties": False}},
        "attribution_ok": {"type": "boolean"},
        "supported": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["claims", "attribution_ok", "supported", "issues"],
    "additionalProperties": False,
}
# The second check also rules on each objection of the first (see verify_request)
SECOND_SCHEMA = {
    **VERIFY_SCHEMA,
    "properties": {**VERIFY_SCHEMA["properties"], "issue_rulings": {"type": "array", "items": {
        "type": "object",
        "properties": {"issue": {"type": "string"}, "valid": {"type": "boolean"},
                       "reason": {"type": "string"}},
        "required": ["issue", "valid", "reason"], "additionalProperties": False}}},
    "required": [*VERIFY_SCHEMA["required"], "issue_rulings"],  # type: ignore[list-item]
}
SECOND_INSTRUCTIONS = (
    "A first fact-checker rejected this script with the objections in first_check_issues. "
    "Check the script yourself with the same rules, and also rule on EACH objection in "
    "issue_rulings: valid (the script really breaks a rule) or not (the checker misread the "
    "script or the record), with a one-line reason. If any objection is valid, the script "
    "is not supported.")
# Left untranslated when an English script is adapted to another language ("According to X,
# un senador..."): never right in a non-English script
ENGLISH_PHRASES = re.compile(r"\b(according to|reports that|the report says|that matters "
                             r"because|some context)\b", re.I)
# Stock lines that make every segment sound the same ("That matters because ..."): the
# consequence is woven into the story instead. Checked in every language.
STOCK_FRAMING = re.compile(
    r"\b(that matters because|this matters because|why (it|this) matters"
    r"|(eso|esto) (importa|es importante) porque|lo importante es que"
    r"|(cela|ceci|c'est) (compte|important) (car|parce)"
    r"|das ist wichtig,? weil|das z\u00e4hlt,? weil"
    r"|(questo|ci\u00f2) (conta|\u00e8 importante) perch\u00e9"
    r"|(isso|isto) (importa|\u00e9 importante) porque)\b", re.I)
# The script describing its source instead of telling the news ("el texto repasa ...")
META = re.compile(
    r"\b((the|this) (article|piece|text|report|story|note) (says|reviews|looks at|recalls|notes|"
    r"explains|highlights|covers|focuses|describes|adds|mentions)"
    r"|(el|este) (texto|art\u00edculo) (dice|repasa|recuerda|explica|destaca|se\u00f1ala|"
    r"describe|menciona|a\u00f1ade|se centra)|la (pieza|nota|noticia|informaci\u00f3n) "
    r"(dice|repasa|recuerda|explica|destaca|funciona|se centra|describe|menciona)"
    r"|(l'article|le texte) (dit|explique|rappelle|souligne|d\u00e9crit|revient)"
    r"|(der|dieser) (artikel|text|bericht) (sagt|erkl\u00e4rt|erinnert|beschreibt|betont)"
    r"|(l'articolo|il testo|il pezzo) (dice|spiega|ricorda|descrive|sottolinea)"
    r"|(o artigo|o texto|a mat\u00e9ria|a reportagem) (diz|explica|lembra|descreve|destaca))\b",
    re.I)
# Background segments never name where the context came from
SOURCE_WORDS = re.compile(r"wikip\u00e9?edia", re.I)
# A figure in the record: 1.5, 15,000, 15.000, 25%, 1922 ...
FIGURE = re.compile(r"\d[\d.,]*")


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w{5,}", text.lower()) if not w.isdigit()}


def uses_background(script: str, story: Story, language: str) -> bool:
    """True if the story has no background in the script's language, or the script uses some
    of it: at least 2 words of the background context the news itself doesn't contain."""
    background = " ".join(b.get("summary", "") for b in story.background or []
                          if (b.get("language") or story.language or "en") == language)
    if not background:
        return True
    own = _words(background) - _words(f"{story.headline} {story.summary} "
                                      f"{story.why_it_matters}")
    return len(own) < 4 or len(own & _words(script)) >= 2  # < 4: nothing usable to add


def figures(text: str) -> set[str]:
    """The numbers in a text, normalised (15.000 = 15,000 = 15000)."""
    return {re.sub(r"[.,]", "", m).lstrip("0") or "0" for m in FIGURE.findall(text)}


# Record fields an outlet actually reported: a claim the script attributes to an outlet must be
# supported by one of these (why_it_matters is the podcast's framing; background is Wikipedia)
REPORTED_FIELDS = {"headline", "summary", "source_name", "also_reported_by", "published"}


def _norm(text: str) -> str:
    text = (text.lower().replace("\u2019", "'").replace("\u2018", "'")
            .replace("\u201c", '"').replace("\u201d", '"'))
    return re.sub(r"\s+", " ", text).strip(" .,;:\"'")


def _record_fields(story: Story) -> dict[str, str]:
    """Each story-record field as normalized text, for checking the checker's evidence."""
    out = {}
    for name, value in story_record(story).items():
        if isinstance(value, list):
            value = " ".join(v if isinstance(v, str) else " ".join(map(str, v.values()))
                             for v in value)
        out[name] = _norm(str(value))
    return out


def _found(evidence: str, fields: dict[str, str]) -> set[str]:
    """Record fields containing the evidence (each part of an "a ... b" quote must be found)."""
    parts = [p for p in (_norm(x) for x in re.split(r"\.\.\.|\u2026", evidence)) if len(p) >= 4]
    return {f for f, text in fields.items() if parts and all(p in text for p in parts)}


def judge(verdict: dict[str, Any], story: Story) -> tuple[bool, list[str]]:
    """Pass/fail from the fact-checker's answer, re-checked in code: a claim marked supported
    needs evidence that is really in the record, and a claim the script attributes to an
    outlet must be supported by what the outlet reported (not why_it_matters/background)."""
    issues = [str(i) for i in verdict.get("issues", []) if str(i).strip()]
    issues += [f"confirmed: {r.get('issue', '')}"[:200] for r in verdict.get("issue_rulings") or []
               if isinstance(r, dict) and r.get("valid") is True]  # 2nd check upheld an objection
    fields = _record_fields(story)
    for c in verdict.get("claims") or []:
        if not isinstance(c, dict) or c.get("supported") is not True:
            continue
        claim = str(c.get("claim", ""))[:140]
        where = _found(str(c.get("evidence", "")), fields)
        if not where:
            issues.append(f"no evidence in the story record for: {claim}")
        elif c.get("attributed") is True and not where & REPORTED_FIELDS:
            issues.append(f"attributed to an outlet but only in the podcast's own framing or "
                          f"background: {claim}")
    if verdict.get("attribution_ok") is False and not any("attribut" in i for i in issues):
        issues.append("attribution error")
    ok = verdict.get("supported") is True and not issues
    if not ok and not issues:
        issues = ["verifier rejected the script"]
    return ok, ([] if ok else issues)


@dataclass
class ClipJob:
    """Work state for one clip that is not in the cache yet."""

    story: Story
    tone: str
    language: str
    voice: str
    script: str = ""
    issues: list[str] = field(default_factory=list)
    verified: bool = False
    cost: float = 0.0
    base: str = ""  # a fact-checked script of the same story to adapt from ("" = write anew)

    @property
    def ref(self) -> str:
        return f"story:{self.story.id}"

    @property
    def words_key(self) -> tuple[int, str, str]:
        """The words depend on story, tone and language, never on the voice."""
        return self.story.id, self.tone, self.language  # type: ignore[return-value]


def words_per_minute(language: str) -> int:
    wpm = pipeline_cfg().clips.words_per_minute
    return wpm.get(language, wpm["default"])


def word_range(language: str) -> tuple[int, int]:
    cfg, wpm = pipeline_cfg().clips, words_per_minute(language)
    return round(cfg.min_seconds * wpm / 60), round(cfg.max_seconds * wpm / 60)


def speech_seconds(text: str, language: str) -> float:
    """Estimated spoken length of `text`."""
    return len(text.split()) / words_per_minute(language) * 60


def story_record(story: Story) -> dict[str, Any]:
    """The only facts a clip may use (background: context from Wikipedia, not news)."""
    record: dict[str, Any] = {
        "headline": story.headline,
        "summary": story.summary,
        "why_it_matters": story.why_it_matters,
        "source_name": story.source_name,
        "published": f"{story.published_at:%Y-%m-%d}",
    }
    if story.status == "explainer":  # background context: no source is named on air
        del record["source_name"], record["published"]
    if story.sources:
        record["also_reported_by"] = [s["name"] for s in story.sources]
    if story.background:
        record["background"] = [{"term": b["term"], "context": b["summary"]}
                                for b in story.background]
    return record


def local_issues(script: str, story: Story, language: str) -> list[str]:
    """Cheap deterministic checks run before the verifier model."""
    issues = []
    lo, hi = word_range(language)
    tol = pipeline_cfg().clips.length_tolerance
    words = len(script.split())
    if not lo * (1 - tol) <= words <= hi * (1 + tol):
        issues.append(f"length {words} words; must be {lo}-{hi}")
    if re.search(r"https?://|www\.|\.com\b", script, re.I):
        issues.append("contains a URL")
    if re.search(r"[*#_`\[\]]|^\s*[-•]\s", script, re.M):
        issues.append("contains markdown")
    if story.status == "explainer":  # background: no source named, no "Wikipedia says"
        if SOURCE_WORDS.search(script):
            issues.append("mentions Wikipedia; background context names no source")
    else:
        names = [n.strip() for n in re.split(r"[()/,]| - ", story.source_name)
                 if len(n.strip()) >= 2]
        if not any(n.lower() in script.lower() for n in names or [story.source_name]):
            issues.append(f"does not attribute the source ({story.source_name})")
        reported = figures(f"{story.headline} {story.summary}")
        if reported and not reported & figures(script):
            issues.append("leaves out the data points: include at least one figure from the "
                          f"record, as digits ({', '.join(sorted(reported)[:5])})")
    if story.status != "explainer":
        if META.search(script):
            issues.append(f"describes the article ({META.search(script).group(0)!r}) instead of "  # type: ignore[union-attr]
                          "telling the news: attribute the facts, then say them directly")
        if SOURCE_WORDS.search(script):
            issues.append("mentions Wikipedia: background context names no source")
        if not uses_background(script, story, language):
            issues.append("no curiosity from background: add one short sentence of context "
                          "or perspective from it, unattributed")
    if STOCK_FRAMING.search(script):
        issues.append(f"uses a stock line ({STOCK_FRAMING.search(script).group(0)!r}): "  # type: ignore[union-attr]
                      "weave the consequence into the story in plain words")
    if language != "en" and ENGLISH_PHRASES.search(script):
        issues.append(f"contains English phrasing in a {language} script: "
                      f"{ENGLISH_PHRASES.search(script).group(0)!r}")  # type: ignore[union-attr]
    return issues


def write_request(job: ClipJob, providers: Providers) -> LLMRequest:
    """Write from the story record, or adapt job.base when the job has one."""
    if job.base:
        return adapt_request(job, providers)
    cfg = pipeline_cfg().clips
    preset = tones_cfg().presets[job.tone]
    lo, hi = word_range(job.language)
    lines = [
        f"Language: {language_names().get(job.language, job.language)}",
        f"Tone: {preset.label}. {preset.instructions}",
        f"Length: {lo}-{hi} words.",
        "Story record (the only facts you may use):",
        json.dumps(story_record(job.story), ensure_ascii=False, indent=1),
    ]
    if job.story.status == "update":
        lines.append("This story updates one covered earlier; say it is an update.")
    if job.story.status == "explainer":
        lines.append("This is a short background explainer, not news: open with a phrase like "
                     "\"Some context:\" in the requested language, explain it plainly in relation "
                     "to the news it follows, and do not present it as something that happened "
                     "today. Never mention Wikipedia or any other source.")
    if job.issues:
        lines.append("A fact-checker rejected the previous draft for these reasons: "
                     + "; ".join(job.issues) + ". Write a corrected version.")
    return LLMRequest(
        purpose="clip_write", model=providers.writer_model, system=load_prompt("script_system.txt"),
        messages=[{"role": "user", "content": "\n".join(lines)}],
        max_tokens=cfg.writer.max_tokens, effort=cfg.writer.effort,
        refusal_fallbacks=cfg.writer.refusal_fallbacks,
        context={"story": story_record(job.story), "story_id": job.story.id, "tone": job.tone,
                 "language": job.language, "words": [lo, hi]},
    )


def adapt_request(job: ClipJob, providers: Providers) -> LLMRequest:
    """Rewrite a fact-checked script of the same story into this job's tone and language."""
    cfg = pipeline_cfg().clips
    preset = tones_cfg().presets[job.tone]
    lo, hi = word_range(job.language)
    lines = [
        f"Language: {language_names().get(job.language, job.language)}",
        f"Tone: {preset.label}. {preset.instructions}",
        f"Length: {lo}-{hi} words.",
        (f"Source publication (keep exactly): {job.story.source_name}"
         if job.story.status != "explainer" else
         "Background context: never mention Wikipedia or any other source."),
        "Story record (the only facts allowed):",
        json.dumps(story_record(job.story), ensure_ascii=False, indent=1),
        "Original segment:",
        job.base,
    ]
    if job.issues:
        lines.append("A fact-checker rejected the previous adaptation for these reasons: "
                     + "; ".join(job.issues) + ". Write a corrected version.")
    return LLMRequest(
        purpose="clip_adapt", model=providers.small_model, system=load_prompt("adapt_system.txt"),
        messages=[{"role": "user", "content": "\n".join(lines)}],
        max_tokens=cfg.adapter.max_tokens, effort=cfg.adapter.effort,
        context={"script": job.base, "story_id": job.story.id, "tone": job.tone,
                 "language": job.language, "words": [lo, hi]},
    )


def verify_request(job: ClipJob, providers: Providers, second: bool = False,
                   first_issues: list[str] | None = None) -> LLMRequest:
    """The fact-check request. second=True: a second opinion by WRITER_MODEL, asked only when
    the first check rejected the script; it rules on each of the first check's objections."""
    cfg = pipeline_cfg().clips
    payload: dict[str, Any] = {"story_record": story_record(job.story), "script": job.script}
    if second:
        payload |= {"first_check_issues": first_issues or [], "instructions": SECOND_INSTRUCTIONS}
    content = json.dumps(payload, ensure_ascii=False, indent=1)
    return LLMRequest(
        purpose="clip_verify_second" if second else "clip_verify",
        model=providers.writer_model if second else providers.small_model,
        system=load_prompt("verify_system.txt"),
        messages=[{"role": "user", "content": content}],
        max_tokens=cfg.verifier.max_tokens, effort=cfg.verifier.effort,
        refusal_fallbacks=cfg.verifier.refusal_fallbacks,
        json_schema=SECOND_SCHEMA if second else VERIFY_SCHEMA,
        context={"story_id": job.story.id},
    )


def _run(session: Session, providers: Providers, stage: str, jobs: list[ClipJob],
         reqs: list[LLMRequest], batch: bool) -> list[LLMResponse | None]:
    """Send requests (optionally as one batch); record costs; retry transient failures once."""
    results = providers.llm.complete_many(reqs, batch=batch)
    out: list[LLMResponse | None] = []
    for job, req, res in zip(jobs, reqs, results, strict=True):
        if isinstance(res, TransientLLMError):
            res = providers.llm.complete_many([req])[0]
        if isinstance(res, Exception):
            log.warning("%s failed for %s: %s", stage, job.ref, res)
            out.append(None)
            continue
        job.cost += record_llm(session, req.purpose, job.ref, res).cost_usd
        out.append(res)
    return out


def _write_and_verify(session: Session, providers: Providers, jobs: list[ClipJob],
                      batch: bool) -> None:
    written = _run(session, providers, "clip_write", jobs,
                   [write_request(j, providers) for j in jobs], batch)
    to_verify: list[ClipJob] = []
    for job, res in zip(jobs, written, strict=True):
        if res is None:
            job.issues = ["writer call failed"]
            continue
        job.script = res.text.strip()
        job.issues = local_issues(job.script, job.story, job.language)
        if not job.issues:
            to_verify.append(job)
    verdicts = _run(session, providers, "clip_verify", to_verify,
                    [verify_request(j, providers) for j in to_verify], batch)
    for job, res in zip(to_verify, verdicts, strict=True):
        job.verified, job.issues = _verdict(res, job)

    # A rejection gets a second opinion (stronger model): it re-checks with the same rules and
    # rules on each objection of the first check. The rejection is overturned only if every
    # objection is ruled invalid and the code checks pass; only failures pay for the call.
    rejected = [j for j in to_verify if not j.verified]
    if rejected and pipeline_cfg().clips.second_opinion:
        seconds = _run(session, providers, "clip_verify_second", rejected,
                       [verify_request(j, providers, second=True, first_issues=j.issues)
                        for j in rejected], batch)
        for job, res in zip(rejected, seconds, strict=True):
            ok, issues = _verdict(res, job)
            if ok:
                metrics.incr("clip_check_overturned")
                job.verified, job.issues = True, []
            else:
                job.issues = list(dict.fromkeys([*issues, *job.issues]))


def _verdict(res: LLMResponse | None, job: ClipJob) -> tuple[bool, list[str]]:
    try:
        return judge(extract_json(res.text) if res else {}, job.story)
    except (ValueError, AttributeError):
        return False, ["verifier output unparseable"]


def _write_and_verify_new(session: Session, providers: Providers, jobs: list[ClipJob],
                          batch: bool) -> None:
    """Write and fact-check new scripts; a rejected draft is rewritten once."""
    if not jobs:
        return
    _write_and_verify(session, providers, jobs, batch)
    retry = [j for j in jobs if not j.verified and j.script]
    if retry:
        metrics.incr("clip_regenerated", len(retry))
        _write_and_verify(session, providers, retry, batch)


def _base_rank(job: ClipJob) -> int:
    """Which job to write from the record first: the story's own language (default tone
    first), then English. A Spanish-sourced story is written in Spanish, then adapted."""
    own = job.story.language or "en"
    return (0 if job.language == own and job.tone == BASE[0] else 1 if job.language == own
            else 2 if job.language == "en" else 3)


def find_base_script(session: Session, story_id: int, story_language: str = "en") -> str:
    """Any fact-checked script of the story, best base first: its own language in the default
    tone, its own language, then English (see _base_rank)."""
    rows = session.exec(select(StoryClip.tone, StoryClip.language, StoryClip.script).where(
        StoryClip.story_id == story_id, StoryClip.verified == True)).all()  # noqa: E712
    ranked = sorted(rows, key=lambda r: (
        0 if r[1] == story_language and r[0] == BASE[0] else 1 if r[1] == story_language
        else 2 if r[1] == "en" else 3))
    return ranked[0][2] if ranked else ""


def _produce_new(session: Session, providers: Providers, jobs: list[ClipJob],
                 batch: bool) -> None:
    """Produce the words for new jobs: once per (story, tone, language), cheapest way first."""
    reps: dict[tuple[int, str, str], ClipJob] = {}
    for job in jobs:
        reps.setdefault(job.words_key, job)  # other voices share these words
    todo = list(reps.values())

    if pipeline_cfg().clips.strategy == "per_combo":
        _write_and_verify_new(session, providers, todo, batch)
    else:
        by_story: dict[int, list[ClipJob]] = {}
        for job in todo:
            by_story.setdefault(job.story.id, []).append(job)  # type: ignore[arg-type]
        bases = {sid: b for sid, js in by_story.items()
                 if (b := find_base_script(session, sid, js[0].story.language or "en"))}
        first = [min(js, key=_base_rank) for sid, js in by_story.items() if sid not in bases]
        _write_and_verify_new(session, providers, first, batch)  # 1. base, from the record
        bases.update({j.story.id: j.script for j in first if j.verified})  # type: ignore[misc]
        first_ids = {id(j) for j in first}
        rest = [j for j in todo if id(j) not in first_ids]
        for job in rest:  # 2. adapt from the base; no verified base -> write from the record
            job.base = bases.get(job.story.id, "")  # type: ignore[arg-type]
        if adapted := sum(1 for j in rest if j.base):
            metrics.incr("clip_adapted", adapted)
        _write_and_verify_new(session, providers, rest, batch)

    for job in jobs:
        rep = reps[job.words_key]
        if rep is not job:
            job.script, job.verified, job.issues = rep.script, rep.verified, list(rep.issues)


def synthesize_clip(session: Session, clip: StoryClip, providers: Providers) -> None:
    """Render a verified clip's script to audio and measure it."""
    audio_dir = get_settings().audio_dir
    name = (f"clips/{providers.tts.voice_tag(clip.voice, clip.language)}/"
            f"story{clip.story_id}_{clip.tone}_{clip.language}_{clip.voice}")
    result = providers.tts.synthesize(clip.script, audio_dir / name, clip.language, clip.voice)
    clip.audio_path = str(result.path.relative_to(audio_dir))
    get_storage().save(clip.audio_path)
    clip.duration_sec = round(result.duration_sec, 2)
    clip.cost_estimate += record_tts(session, "clip_tts", f"story:{clip.story_id}",
                                     providers.tts.name, result.chars).cost_usd
    session.add(clip)


def find_clip(session: Session, story_id: int, tone: str, language: str,
              voice: str) -> StoryClip | None:
    return session.exec(select(StoryClip).where(
        StoryClip.story_id == story_id, StoryClip.tone == tone,
        StoryClip.language == language, StoryClip.voice == voice)).first()


def find_checked_script(session: Session, story_id: int, tone: str, language: str) -> str:
    """A script already fact-checked for this story, tone and language in another voice.
    The words don't depend on the voice, so they're reused and only the audio is recorded."""
    row = session.exec(select(StoryClip.script).where(
        StoryClip.story_id == story_id, StoryClip.tone == tone,
        StoryClip.language == language, StoryClip.verified == True)).first()  # noqa: E712
    story = session.get(Story, story_id)
    return row if row and story and not stale(row, story, language) else ""


def stale(script: str, story: Story, language: str) -> bool:
    """A cached script that breaks a format rule added since it was written (free re-check;
    length is left out: it was checked against the word range of its time)."""
    return any(not i.startswith("length") for i in local_issues(script, story, language))


def drop_stale_clips(session: Session, story: Story, tone: str, language: str) -> int:
    """Delete this story's cached clips in this tone and language (every voice) whose script
    breaks a current rule, so they are written and checked again."""
    rows = session.exec(select(StoryClip).where(
        StoryClip.story_id == story.id, StoryClip.tone == tone, StoryClip.language == language,
        StoryClip.verified == True)).all()  # noqa: E712
    gone = [c for c in rows if stale(c.script, story, language)]
    for clip in gone:
        log.info("Rewriting cached script of story %s (%s/%s): %s", story.id, tone, language,
                 "; ".join(local_issues(clip.script, story, language)))
        session.delete(clip)
    if gone:
        session.commit()
    return len(gone)


def ensure_clips(session: Session, specs: list[tuple[Story, str, str, str]],
                 providers: Providers, batch: bool = False,
                 synthesize: bool = True) -> list[StoryClip]:
    """Return clips for (story, tone, language, voice) specs, generating only cache misses.

    Writer/verifier calls for all misses are grouped per phase so they can go through the
    batch API. Specs whose generation failed transiently are omitted from the result.
    """
    clips: dict[int, StoryClip] = {}
    jobs: dict[int, ClipJob] = {}
    for i, (story, tone, language, voice) in enumerate(specs):
        tone = tones_cfg().preset_for(tone)
        cached = find_clip(session, story.id, tone, language, voice)  # type: ignore[arg-type]
        if cached and cached.verified and stale(cached.script, story, language):
            drop_stale_clips(session, story, tone, language)  # rules changed: write again
            cached = None
        if cached:
            metrics.incr("clip_cache_hit")
            clips[i] = cached
        else:
            metrics.incr("clip_cache_miss")
            jobs[i] = ClipJob(story, tone, language, voice)
            script = find_checked_script(session, story.id, tone, language)  # type: ignore[arg-type]
            if script:  # same words, new voice: skip writing and fact-checking
                metrics.incr("clip_script_reused")
                jobs[i].script, jobs[i].verified = script, True

    if jobs:
        _produce_new(session, providers, [j for j in jobs.values() if not j.script], batch)

        for i, job in jobs.items():
            if not job.script:
                continue  # writer never produced text (transient failure): try again later
            clip = StoryClip(story_id=job.story.id, tone=job.tone, language=job.language,  # type: ignore[arg-type]
                             voice=job.voice, script=job.script, verified=job.verified,
                             verification_notes="; ".join(job.issues), cost_estimate=job.cost)
            try:
                with session.begin_nested():
                    session.add(clip)
            except IntegrityError:  # another worker created it meanwhile
                clip = find_clip(session, job.story.id, job.tone, job.language, job.voice)  # type: ignore[arg-type,assignment]
            if not job.verified:
                metrics.incr("clip_unverified")
                log.warning("Clip for %s failed verification: %s", job.ref, clip.verification_notes)
            clips[i] = clip  # type: ignore[assignment]
        session.commit()

    if synthesize:
        for clip in clips.values():
            if clip.verified and not recorded_by(clip.audio_path, providers.tts, clip.voice,
                                                 clip.language):
                synthesize_clip(session, clip, providers)
        session.commit()
    return [clips[i] for i in sorted(clips)]


def ensure_clip(session: Session, story: Story, tone: str, language: str, voice: str,
                providers: Providers, synthesize: bool = True) -> StoryClip:
    """Single-clip convenience wrapper (cache hit returns immediately)."""
    result = ensure_clips(session, [(story, tone, language, voice)], providers,
                          synthesize=synthesize)
    if not result:
        raise TransientLLMError(f"could not generate clip for story {story.id}")
    return result[0]
