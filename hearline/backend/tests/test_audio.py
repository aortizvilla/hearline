"""Step 6: the stitched episode meets the podcast loudness standard."""

import json
import subprocess

import pytest
from pydub.generators import Sine, WhiteNoise

from app.pipeline.step6_audio import TARGET_LUFS, TRUE_PEAK_DB, ffmpeg_available, stitch

pytestmark = pytest.mark.skipif(not ffmpeg_available(), reason="needs ffmpeg")


def _measure(path):
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", "loudnorm=print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, check=True).stderr
    return json.loads(out[out.rindex("{"):out.rindex("}") + 1])


def test_stitch_levels_episode_to_podcast_loudness(tmp_path):
    # a quiet tone, a loud noisy segment and a mid one: very different loudness
    parts = []
    for i, seg in enumerate([
        Sine(220).to_audio_segment(duration=4000, volume=-35),
        WhiteNoise().to_audio_segment(duration=4000, volume=-6),
        Sine(440).to_audio_segment(duration=4000, volume=-20).overlay(
            WhiteNoise().to_audio_segment(duration=4000, volume=-30)),
    ]):
        p = tmp_path / f"part{i}.wav"
        seg.set_frame_rate(24000).export(str(p), format="wav")
        parts.append(p)

    out, seconds = stitch(parts, tmp_path / "ep", "Title", "Desc")

    assert out.suffix == ".mp3" and seconds > 12
    stats = _measure(out)
    assert abs(float(stats["input_i"]) - TARGET_LUFS) <= 1.0
    assert float(stats["input_tp"]) <= TRUE_PEAK_DB + 0.5  # MP3 decode adds a little overshoot


def test_sounds_play_under_the_voice(tmp_path, monkeypatch):
    from app.pipeline import step6_audio

    sounds = tmp_path / "sounds"
    sounds.mkdir()
    monkeypatch.setattr(step6_audio, "SOUNDS_DIR", sounds)
    Sine(880).to_audio_segment(duration=1000, volume=-3).export(str(sounds / "intro.wav"),
                                                                 format="wav")
    assert step6_audio.sound("intro") == sounds / "intro.wav"
    assert step6_audio.sound("outro") is None

    voice = tmp_path / "voice.wav"
    Sine(220).to_audio_segment(duration=3000, volume=-30).set_frame_rate(24000).export(
        str(voice), format="wav")
    out, seconds = stitch([sounds / "intro.wav", voice], tmp_path / "ep", "T", "D",
                          gap_ms=0, export_format="wav")

    from pydub import AudioSegment
    ep = AudioSegment.from_file(str(out))
    assert ep.frame_rate == 24000  # the voice's rate wins
    assert ep[:900].dBFS < ep[1200:].dBFS - 6  # the sting sits ~8 dB under the voice
