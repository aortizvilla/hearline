"""`python -m app.cli check`: confirm keys, models, prices and voices work. Free: no generation."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings, pipeline_cfg, pricing_cfg


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _run(name: str, fn: Callable[[], str]) -> Check:
    try:
        return Check(name, True, fn())
    except Exception as exc:  # noqa: BLE001 - every failure is reported, none is fatal
        return Check(name, False, f"{type(exc).__name__}: {exc}"[:300])


def _models() -> dict[str, str]:
    s = get_settings()
    return {"RESEARCHER_MODEL": s.researcher_model, "WRITER_MODEL": s.writer_model,
            "SMALL_MODEL": s.small_model}


def check_llm() -> list[Check]:
    """Each configured model exists for this key (a metadata lookup, not a generation)."""
    s = get_settings()
    if s.llm_provider == "mock":
        return [Check("LLM", True, "mock provider, nothing to check")]
    try:
        if s.llm_provider == "openai":
            import openai

            client: Any = openai.OpenAI(api_key=s.openai_api_key or None)
        else:
            import anthropic

            client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    except Exception as exc:  # noqa: BLE001 - e.g. no API key yet
        return [Check(f"LLM {s.llm_provider}", False, f"{type(exc).__name__}: {exc}"[:300])]
    return [_run(f"LLM {var}={model}", lambda m=model: f"available on {s.llm_provider}"
                 if client.models.retrieve(m) else "")
            for var, model in _models().items()]


def check_prices() -> list[Check]:
    """Every model and the TTS voice has a price, so cost reports and the budget cap are right."""
    llm_prices = pricing_cfg().llm
    out = []
    for var, model in _models().items():
        known = any(model.startswith(k) for k in llm_prices if k != "default")
        out.append(Check(f"Price {var}", known, "found in pricing.yaml" if known else
                         f"{model!r} missing in config/pricing.yaml (falls back to `default`)"))
    return out


def check_tts() -> list[Check]:
    """Every UI voice resolves to a real provider voice in every language."""
    from app.providers import make_tts
    from app.providers.tts import ElevenLabsTTSProvider, PollyTTSProvider

    cfg = pipeline_cfg().tts
    try:
        tts = make_tts(cfg.provider)
    except Exception as exc:  # noqa: BLE001
        return [Check(f"TTS {cfg.provider}", False, str(exc))]
    price = Check(f"Price TTS {tts.name}", tts.name in pricing_cfg().tts_per_1m_chars,
                  "found in pricing.yaml" if tts.name in pricing_cfg().tts_per_1m_chars
                  else "missing in config/pricing.yaml (falls back to `default`)")

    if isinstance(tts, PollyTTSProvider):
        def polly() -> str:
            pages = tts.client.get_paginator("describe_voices").paginate(Engine=tts.engine)
            available = {v["Id"] for page in pages for v in page["Voices"]}
            wanted = {(name, lang, vid) for name, v in cfg.voices.items()
                      for lang, vid in v.get("polly", {}).items()}
            missing = sorted(f"{n}/{lang}={vid}" for n, lang, vid in wanted if vid not in available)
            if missing:
                raise ValueError(f"not available with engine {tts.engine}: {', '.join(missing)}")
            return f"{len(wanted)} voice/language pairs available with engine {tts.engine}"
        return [price, _run("TTS Polly voices", polly)]

    if isinstance(tts, ElevenLabsTTSProvider):
        def eleven() -> str:
            wanted = {(name, "default", v.get("elevenlabs")) for name, v in cfg.voices.items()}
            wanted |= {(name, lang, vid) for name, v in cfg.voices.items()
                       for lang, vid in v.get("elevenlabs_by_language", {}).items()}
            bad = []
            for name, lang, vid in sorted(wanted, key=str):
                r = httpx.get(f"https://api.elevenlabs.io/v1/voices/{vid}",
                              headers={"xi-api-key": tts.api_key}, timeout=20)
                if r.status_code != 200:
                    bad.append(f"{name}/{lang} ({r.status_code})")
            if bad:
                raise ValueError("voices not in this account (add them from the Voice Library): "
                                 + ", ".join(bad))
            return f"{len(wanted)} voice/language pairs found, model {tts.model_id}"
        return [price, _run("TTS ElevenLabs voices", eleven)]

    return [price, Check(f"TTS {tts.name}", True, "local provider, nothing to check")]


def check_ffmpeg() -> list[Check]:
    ok = shutil.which("ffmpeg") is not None
    return [Check("ffmpeg", ok, "installed (MP3 export)" if ok else "missing: episodes fall "
                  "back to WAV without tags (brew install ffmpeg)")]


def run_checks() -> list[Check]:
    return check_llm() + check_prices() + check_tts() + check_ffmpeg()
