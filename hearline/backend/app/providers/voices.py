"""The voice catalog: config/voices.yaml -> the dim_voice table, plus one preview per voice.

seed_voices() runs on startup (like seed_topics): new voices are added, changed labels or
provider mappings are updated, and voices removed from the file are deactivated (kept, because
old profiles may still point at them). The TTS providers read the same file's mapping through
pipeline_cfg().tts.voices.

record_previews() records `preview_text` once per active voice and language with the current
TTS provider (`python -m app.cli voice-previews`); Settings plays the one for the language the
listener selected. A remapped voice records again.
"""

from __future__ import annotations

from sqlmodel import Session, col, select

from app.config import get_settings, language_names, voices_cfg
from app.database.models import Voice, utcnow
from app.providers.storage import get_storage
from app.providers.tts import TTSProvider, recorded_by
from app.tracking.costs import record_tts


def seed_voices(session: Session) -> int:
    """Sync dim_voice with config/voices.yaml. Returns the number of new voices."""
    entries = voices_cfg().voices
    existing = {v.key: v for v in session.exec(select(Voice))}
    new = 0
    for order, e in enumerate(entries):
        voice = existing.get(e.key)
        if voice is None:
            voice, new = Voice(key=e.key, label=e.label), new + 1
        fields = {"label": e.label, "style": e.style, "gender": e.gender, "accent": e.accent,
                  "accents": e.accents(), "active": e.active, "sort_order": order,
                  "providers": e.providers()}
        if any(getattr(voice, k) != v for k, v in fields.items()):
            for k, v in fields.items():
                setattr(voice, k, v)
            voice.updated_at = utcnow()
            session.add(voice)
    listed = {e.key for e in entries}
    for key, voice in existing.items():
        if key not in listed and voice.active:
            voice.active, voice.updated_at = False, utcnow()
            session.add(voice)
    session.commit()
    return new


def active_voices(session: Session) -> list[Voice]:
    return list(session.exec(select(Voice).where(Voice.active == True)  # noqa: E712
                             .order_by(col(Voice.sort_order))))


def record_previews(session: Session, tts: TTSProvider, force: bool = False) -> list[str]:
    """Record the sample of every active voice in every Settings language that has a
    `preview_text` and no current recording. Returns what was recorded ("aria/es", ...)."""
    texts, audio_dir, done = voices_cfg().preview_text, get_settings().audio_dir, []
    for voice in active_voices(session):
        previews = dict(voice.previews or {})
        for lang in language_names():
            if lang not in texts or (
                    not force and recorded_by(previews.get(lang), tts, voice.key, lang)):
                continue
            rel = f"voices/{tts.voice_tag(voice.key, lang)}/{lang}/{voice.key}"
            result = tts.synthesize(texts[lang], audio_dir / rel, lang, voice.key)
            previews[lang] = str(result.path.relative_to(audio_dir))
            get_storage().save(previews[lang])
            record_tts(session, "voice_preview", f"voice:{voice.key}", tts.name, result.chars)
            voice.previews = previews  # a new dict, so the JSON column is saved
            session.add(voice)
            session.commit()
            done.append(f"{voice.key}/{lang}")
    return done
