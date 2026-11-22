"""Voice catalog: config/voices.yaml -> dim_voice -> Settings, previews, and re-recording."""

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.config import voices_cfg
from app.database.models import Voice
from app.providers import Providers
from app.providers.tts import MockTTSProvider, recorded_by
from app.providers.voices import active_voices, record_previews, seed_voices


def test_seed_loads_the_file_and_deactivates_removed_voices(session: Session,
                                                             monkeypatch) -> None:  # type: ignore[no-untyped-def]
    assert seed_voices(session) == len(voices_cfg().voices)
    assert seed_voices(session) == 0  # idempotent
    keys = [v.key for v in active_voices(session)]
    assert keys[:4] == ["aria", "leo", "nova", "kai"] and "oliver" in keys

    trimmed = voices_cfg().model_copy(update={"voices": voices_cfg().voices[1:]})
    monkeypatch.setattr("app.providers.voices.voices_cfg", lambda: trimmed)
    seed_voices(session)
    aria = session.exec(select(Voice).where(Voice.key == "aria")).one()
    assert aria.active is False  # kept for old profiles, no longer offered
    assert "aria" not in [v.key for v in active_voices(session)]


def test_settings_lists_voices_from_the_database(client: TestClient) -> None:
    voices = client.get("/api/options").json()["voices"]
    first = voices[0]
    assert (first["value"], first["label"], first["accent"]) == ("aria", "Aria", "American")
    assert first["description"] == "Warm and professional" and first["previews"] == {}

    profile = client.get("/api/profile").json()
    assert client.put("/api/profile", json={**profile, "voice": "oliver"}).status_code == 200
    assert client.put("/api/profile", json={**profile, "voice": "robot"}).status_code == 422


def test_previews_are_recorded_once_per_language_and_served(
        client: TestClient, session: Session, providers: Providers) -> None:
    from app.config import language_names

    langs = set(language_names())
    done = record_previews(session, providers.tts)
    assert len(done) == len(active_voices(session)) * len(langs)  # every voice x language
    assert record_previews(session, providers.tts) == []  # already current
    previews = client.get("/api/options").json()["voices"][0]["previews"]
    assert set(previews) == langs
    assert previews["es"] == "/audio/voices/mock/es/aria.wav"


def test_remapping_a_provider_voice_records_again() -> None:
    from app.providers.tts import ElevenLabsTTSProvider

    old = ElevenLabsTTSProvider("k", "eleven_flash_v2_5", {"aria": {"elevenlabs": "OLD"}})
    new = ElevenLabsTTSProvider("k", "eleven_flash_v2_5", {"aria": {"elevenlabs": "NEW"}})
    path = f"clips/{old.voice_tag('aria', 'en')}/story1_conversational_en_aria.mp3"
    assert recorded_by(path, old, "aria", "en")
    assert not recorded_by(path, new, "aria", "en")  # same provider, different voice
    assert not recorded_by(path, MockTTSProvider(), "aria", "en")


def test_each_language_uses_its_native_speaker_and_accent() -> None:
    from app.config import VoiceEntry
    from app.providers.tts import ElevenLabsTTSProvider

    entry = VoiceEntry.model_validate({
        "key": "oliver", "label": "Oliver", "accent": "British", "elevenlabs": "EN_ID",
        "languages": {"es": {"elevenlabs": "ES_ID", "accent": "Spain", "locale": "es-ES"},
                      "pt": {"elevenlabs": "PT_ID", "accent": "Portugal"}}})
    assert entry.accents() == {"en": "British", "es": "Spain", "pt": "Portugal"}
    tts = ElevenLabsTTSProvider("k", "eleven_flash_v2_5", {"oliver": entry.providers()})
    assert tts.voice_id("oliver", "es") == "ES_ID"
    assert tts.voice_id("oliver", "de") == "EN_ID"  # no native speaker configured: default
    assert tts.voice_tag("oliver", "pt").endswith("/PT_ID")  # each language its own recordings
