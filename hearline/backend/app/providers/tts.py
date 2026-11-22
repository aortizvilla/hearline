"""Text-to-speech: a TTSProvider interface and pluggable implementations."""

from __future__ import annotations

import math
import random
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import wave
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass
class TTSResult:
    path: Path
    duration_sec: float
    chars: int


def measure_duration(path: Path) -> float:
    """Audio duration in seconds (WAV via stdlib, other formats via mutagen)."""
    if path.suffix.lower() == ".wav":
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    from mutagen import File as MutagenFile

    info = MutagenFile(str(path))
    if info is None or info.info is None:
        raise ValueError(f"cannot read audio duration of {path}")
    return float(info.info.length)


def recorded_by(audio_path: str | None, provider: TTSProvider, voice: str,
                language: str) -> bool:
    """True if this stored audio was made by `provider` with the provider voice that `voice`
    maps to now (both are folders in the path). Switching provider or engine, or remapping a
    voice in pipeline.yaml, therefore re-records instead of reusing the old sound."""
    return bool(audio_path) and f"/{provider.voice_tag(voice, language)}/" in f"/{audio_path}"


class _NoLimit:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


def request_slots(max_concurrent: int) -> threading.BoundedSemaphore | _NoLimit:
    """Caps simultaneous requests to a paid voice API across this process's worker threads
    (tts.max_concurrent_requests; 0 = no limit). Waiting here is cheaper than a 429 retry."""
    return threading.BoundedSemaphore(max_concurrent) if max_concurrent > 0 else _NoLimit()


class TTSProvider(ABC):
    """Interface for converting text into an audio file."""

    name: str = "base"
    extension: str = ".wav"

    def voice_tag(self, voice: str, language: str) -> str:
        """Folder for this provider + the provider voice behind `voice` (see recorded_by)."""
        return self.name

    @abstractmethod
    def synthesize(
        self, text: str, output_path: Path, language: str = "en", voice: str = "aria"
    ) -> TTSResult:
        """Render `text` to audio at `output_path` (suffix may be adjusted) and measure it."""


class MockTTSProvider(TTSProvider):
    """Writes a quiet tone whose length matches natural speech pace. No network, no cost."""

    name = "mock"

    def __init__(self, sample_rate: int = 8000, words_per_minute: int = 150) -> None:
        self.sample_rate = sample_rate
        self.wpm = words_per_minute

    def synthesize(
        self, text: str, output_path: Path, language: str = "en", voice: str = "aria"
    ) -> TTSResult:
        output_path = output_path.with_suffix(".wav")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        seconds = max(1.0, len(text.split()) / self.wpm * 60)
        n = int(seconds * self.sample_rate)
        freq = 180 + (sum(map(ord, voice)) % 80)
        amp = 2500
        frames = b"".join(
            struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / self.sample_rate)))
            for i in range(0, min(n, self.sample_rate))  # one second period, then repeat
        )
        reps, rest = divmod(n, self.sample_rate)
        data = frames * reps + frames[: rest * 2]
        with wave.open(str(output_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            w.writeframes(data)
        return TTSResult(output_path, n / self.sample_rate, len(text))


class SayTTSProvider(TTSProvider):
    """macOS `say` command: real speech, local and free (voice names from config)."""

    name = "say"

    def __init__(self, voices: dict[str, dict[str, Any]], sample_rate: int = 22050) -> None:
        if shutil.which("say") is None:
            raise RuntimeError("`say` is only available on macOS")
        self.voices = voices
        self.sample_rate = sample_rate

    def _voice(self, voice: str, language: str) -> str | None:
        entry = self.voices.get(voice, {})
        return entry.get("say_by_language", {}).get(language) or entry.get("say")

    def voice_tag(self, voice: str, language: str) -> str:
        return f"{self.name}/{self._voice(voice, language) or 'default'}"

    def synthesize(
        self, text: str, output_path: Path, language: str = "en", voice: str = "aria"
    ) -> TTSResult:
        output_path = output_path.with_suffix(".wav")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(text)
            text_file = f.name
        cmd = ["say", "-o", str(output_path), "--file-format=WAVE",
               f"--data-format=LEI16@{self.sample_rate}", "-f", text_file]
        sys_voice = self._voice(voice, language)
        if sys_voice:
            cmd[1:1] = ["-v", sys_voice]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        except subprocess.CalledProcessError:
            if not sys_voice:
                raise
            cmd = [c for c in cmd if c not in ("-v", sys_voice)]  # voice not installed
            subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        finally:
            Path(text_file).unlink(missing_ok=True)
        return TTSResult(output_path, measure_duration(output_path), len(text))


def split_text(text: str, limit: int) -> list[str]:
    """Split text into chunks of at most `limit` characters, at sentence ends when possible."""
    chunks, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        while len(sentence) > limit:  # a single very long sentence: hard split
            chunks.append(sentence[:limit])
            sentence = sentence[limit:]
        if current and len(current) + 1 + len(sentence) > limit:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


class PollyTTSProvider(TTSProvider):
    """Amazon Polly. Credentials: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION in .env
    (or any standard AWS credential source). Voices per language come from config."""

    extension = ".mp3"
    MAX_CHARS = 2900  # Polly bills up to 3,000 characters per request; longer text is split

    def __init__(self, engine: str, voices: dict[str, dict[str, Any]], region: str = "",
                 access_key_id: str = "", secret_access_key: str = "",
                 client: Any = None, max_concurrent: int = 0) -> None:
        self.engine, self.voices = engine, voices
        self.slots = request_slots(max_concurrent)
        self.name = f"polly-{engine}"  # e.g. polly-neural; also the price key in pricing.yaml
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(  # adaptive retries slow down by themselves when throttled
                "polly", region_name=region or None,
                aws_access_key_id=access_key_id or None,
                aws_secret_access_key=secret_access_key or None,
                config=Config(retries={"mode": "adaptive", "max_attempts": 8}))
        self.client = client

    def voice_id(self, voice: str, language: str) -> str:
        voice_id = self.voices.get(voice, {}).get("polly", {}).get(language)
        if not voice_id:
            raise ValueError(f"no Polly voice configured for {voice!r} in {language!r}")
        return str(voice_id)

    def voice_tag(self, voice: str, language: str) -> str:
        return f"{self.name}/{self.voice_id(voice, language)}"

    def synthesize(
        self, text: str, output_path: Path, language: str = "en", voice: str = "aria"
    ) -> TTSResult:
        output_path = output_path.with_suffix(".mp3")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        voice_id = self.voice_id(voice, language)
        audio = b""
        for chunk in split_text(text, self.MAX_CHARS):  # MP3 frames can be concatenated
            with self.slots:
                resp = self.client.synthesize_speech(Text=chunk, OutputFormat="mp3",
                                                     VoiceId=voice_id, Engine=self.engine)
                audio += resp["AudioStream"].read()
        output_path.write_bytes(audio)
        return TTSResult(output_path, measure_duration(output_path), len(text))


# ElevenLabs models that accept `language_code` (others reject it, so it is not sent)
ELEVENLABS_LANGUAGE_CODE_MODELS = {"eleven_flash_v2_5", "eleven_turbo_v2_5"}


class ElevenLabsTTSProvider(TTSProvider):
    """ElevenLabs HTTP API (ELEVENLABS_API_KEY; model id and voice ids from config)."""

    extension = ".mp3"
    URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    MAX_ATTEMPTS = 6

    def __init__(self, api_key: str, model_id: str, voices: dict[str, dict[str, Any]],
                 max_concurrent: int = 0) -> None:
        if not api_key or not model_id:
            raise RuntimeError("ElevenLabs needs ELEVENLABS_API_KEY and tts.elevenlabs_model")
        self.api_key, self.model_id, self.voices = api_key, model_id, voices
        self.slots = request_slots(max_concurrent)
        self.name = f"elevenlabs-{model_id}"  # price key in pricing.yaml

    def voice_id(self, voice: str, language: str = "en") -> str:
        """The native speaker for this language if configured, else the voice's default."""
        entry = self.voices.get(voice, {})
        voice_id = entry.get("elevenlabs_by_language", {}).get(language) or entry.get("elevenlabs")
        if not voice_id:
            raise ValueError(f"no ElevenLabs voice configured for {voice!r}")
        return str(voice_id)

    def voice_tag(self, voice: str, language: str) -> str:
        return f"{self.name}/{self.voice_id(voice, language)}"

    def synthesize(
        self, text: str, output_path: Path, language: str = "en", voice: str = "aria"
    ) -> TTSResult:
        output_path = output_path.with_suffix(".mp3")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        voice_id = self.voice_id(voice, language)
        for attempt in range(self.MAX_ATTEMPTS):
            with self.slots:  # at most tts.max_concurrent_requests at once in this process
                resp = httpx.post(
                    self.URL.format(voice_id=voice_id),
                    params={"output_format": "mp3_44100_128"},
                    headers={"xi-api-key": self.api_key},
                    json={"text": text, "model_id": self.model_id,
                          **({"language_code": language}
                             if self.model_id in ELEVENLABS_LANGUAGE_CODE_MODELS else {})},
                    timeout=120,
                )
            # 429 = over the plan's concurrent-request limit; 5xx = temporary
            if resp.status_code != 429 and resp.status_code < 500:
                break
            if attempt < self.MAX_ATTEMPTS - 1:
                time.sleep(min(30.0, 2 ** attempt) * (0.8 + 0.4 * random.random()))
        resp.raise_for_status()
        output_path.write_bytes(resp.content)
        return TTSResult(output_path, measure_duration(output_path), len(text))

