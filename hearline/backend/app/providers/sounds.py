"""Generate the shared episode sounds (hearline/backend/sounds/) once with ElevenLabs Sound Effects.

Each clip is made once and reused by every episode (step 6), so this is a one-off cost.
Run: python -m app.cli sounds [--force]
"""

from __future__ import annotations

import logging

import httpx

from app.config import get_settings
from app.pipeline.step6_audio import SOUNDS_DIR, sound

log = logging.getLogger(__name__)

URL = "https://api.elevenlabs.io/v1/sound-generation"

# name -> (prompt, seconds). Calm, modern news-briefing feel; no vocals.
SOUNDS: dict[str, tuple[str, float]] = {
    "intro": ("Short modern news podcast intro music sting, warm synth pads and soft "
              "electronic pulse rising to a bright chime, confident and calm, no vocals", 4.0),
    "transition": ("Clear podcast segment change: a bright three-note rising synth motif "
                   "with a soft whoosh and a short warm pad tail, distinct and confident, "
                   "no vocals", 2.5),
    "outro": ("Gentle podcast outro music tail, warm synth pad chord with soft piano notes "
              "slowly fading out to silence, calm, no vocals", 6.0),
}


def generate_sounds(force: bool = False) -> list[str]:
    """Write intro / transition / outro .mp3 into SOUNDS_DIR. Returns the names written."""
    api_key = get_settings().elevenlabs_api_key
    if not api_key:
        raise RuntimeError("set ELEVENLABS_API_KEY in .env")
    SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for name, (prompt, seconds) in SOUNDS.items():
        if sound(name) and not force:
            log.info("%s already exists, skipping (use --force to regenerate)", name)
            continue
        resp = httpx.post(URL, params={"output_format": "mp3_44100_128"},
                          headers={"xi-api-key": api_key}, timeout=120,
                          json={"text": prompt, "duration_seconds": seconds,
                                "prompt_influence": 0.5})
        if resp.status_code != 200:
            raise RuntimeError(f"ElevenLabs {name}: HTTP {resp.status_code} {resp.text[:300]}")
        (SOUNDS_DIR / f"{name}.mp3").write_bytes(resp.content)
        written.append(name)
    return written
