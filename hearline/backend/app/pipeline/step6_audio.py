"""Step 6: stitch greeting, transitions, story audio and outro into one episode file.

Audio is already recorded by steps 3 and 5; this only joins it (pydub + ffmpeg, no paid
call): optional intro / transition / outro sounds from hearline/backend/sounds/, 600 ms gaps,
loudness matched per segment, then the whole episode is levelled to the podcast standard
(-16 LUFS integrated, -1 dBTP true peak) with ffmpeg's two-pass loudnorm and saved as MP3
with ID3 tags. Without ffmpeg it falls back to a WAV levelled by average level.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from pydub import AudioSegment

from app.config import BACKEND_DIR

log = logging.getLogger(__name__)

TARGET_LUFS = -16.0   # podcast standard (Apple / Spotify), integrated loudness
TRUE_PEAK_DB = -1.0   # dBTP ceiling, leaves headroom for MP3 encoding
LOUDNESS_RANGE = 11.0

SOUNDS_DIR = BACKEND_DIR / "sounds"  # royalty-free intro / transition / outro (see its README)
SOUND_EXTENSIONS = (".mp3", ".wav", ".ogg", ".flac", ".m4a")


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def sound(name: str) -> Path | None:
    """The shared sound clip `name` (intro, transition, outro) if one is in SOUNDS_DIR."""
    return next((p for ext in SOUND_EXTENSIONS if (p := SOUNDS_DIR / f"{name}{ext}").is_file()),
                None)


def _load(path: Path, frame_rate: int, target_dbfs: float) -> AudioSegment:
    seg = AudioSegment.from_file(str(path)).set_channels(1).set_frame_rate(frame_rate)
    if seg.dBFS != float("-inf"):
        seg = seg.apply_gain(target_dbfs - seg.dBFS)  # match loudness across voices/segments
    return seg


def stitch(
    parts: list[Path],
    out_path: Path,
    title: str,
    description: str,
    gap_ms: int = 600,
    crossfade_ms: int = 150,
    target_dbfs: float = -16.0,
    sfx_offset_db: float = -8.0,
    export_format: str = "mp3",
    bitrate: str = "96k",
    sfx_offsets_db: dict[str, float] | None = None,
    topic_pause_ms: int = 0,
) -> tuple[Path, float]:
    """Concatenate audio parts with short gaps/fades. Returns (written path, seconds).

    sfx_offsets_db: per-sound level under the voice (by file name, e.g. {"transition": -3});
    others use sfx_offset_db. Sounds are never crossfaded into speech, and the topic-change
    sound gets topic_pause_ms of silence before it, so a new topic is clearly heard."""
    if not parts:
        raise ValueError("nothing to stitch")
    rate = max(AudioSegment.from_file(str(p)).frame_rate for p in parts
               if p.parent != SOUNDS_DIR)  # the voice's rate, not the music's
    episode = AudioSegment.empty()
    for path in parts:
        is_sfx = path.parent == SOUNDS_DIR  # music/effects sit under the voice
        offset = (sfx_offsets_db or {}).get(path.stem, sfx_offset_db) if is_sfx else 0.0
        seg = _load(path, rate, target_dbfs + offset)
        if len(episode):
            pause = gap_ms + (topic_pause_ms if is_sfx and path.stem == "transition" else 0)
            episode += AudioSegment.silent(duration=pause, frame_rate=rate)
            fade = 0 if is_sfx else min(crossfade_ms, len(seg) // 2, gap_ms)
            episode = episode.append(seg, crossfade=fade) if fade > 0 else episode + seg
        else:
            episode = seg
    out_path.parent.mkdir(parents=True, exist_ok=True)
    seconds = len(episode) / 1000.0
    if export_format == "mp3" and ffmpeg_available():
        out_path = out_path.with_suffix(".mp3")
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "episode.wav"
            episode.export(str(wav), format="wav")
            _loudnorm_to_mp3(wav, out_path, rate, bitrate)
        tag_mp3(out_path, title, description)
        return out_path, seconds

    if export_format == "mp3":
        log.warning("ffmpeg not found: exporting WAV (no ID3 tags, no LUFS levelling)")
        export_format = "wav"
    if episode.max_dBFS > TRUE_PEAK_DB:  # keep peaks under -1 dBFS after gain matching
        episode = episode.apply_gain(TRUE_PEAK_DB - episode.max_dBFS)
    out_path = out_path.with_suffix(f".{export_format}")
    episode.export(str(out_path), format=export_format)
    return out_path, seconds


def _loudnorm_to_mp3(wav: Path, out_path: Path, rate: int, bitrate: str) -> None:
    """Two-pass EBU R128 loudnorm: measure, then apply one linear gain to hit the target."""
    target = f"I={TARGET_LUFS}:TP={TRUE_PEAK_DB}:LRA={LOUDNESS_RANGE}"
    probe = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(wav),
         "-af", f"loudnorm={target}:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, check=True)
    stats = json.loads(probe.stderr[probe.stderr.rindex("{"):probe.stderr.rindex("}") + 1])
    measured = (f"measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
                f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
                f":offset={stats['target_offset']}:linear=true")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-y", "-i", str(wav),
         "-af", f"loudnorm={target}:{measured}", "-ar", str(rate), "-ac", "1",
         "-codec:a", "libmp3lame", "-b:a", bitrate, str(out_path)],
        check=True)


def tag_mp3(path: Path, title: str, description: str) -> None:
    """Write ID3 title / description / artist / album tags."""
    from mutagen.id3 import COMM, ID3, TALB, TIT2, TPE1, ID3NoHeaderError

    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()
    tags.add(TIT2(encoding=3, text=title))
    tags.add(COMM(encoding=3, lang="eng", desc="description", text=description))
    tags.add(TPE1(encoding=3, text="Hearline"))
    tags.add(TALB(encoding=3, text="Hearline Daily Briefing"))
    tags.save(str(path))
