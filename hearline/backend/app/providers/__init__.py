"""Pluggable providers (LLM, embeddings, vector search, TTS) and their factory."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from app.config import get_settings, pipeline_cfg
from app.providers.embeddings import EmbeddingProvider, make_embedder
from app.providers.llm import AnthropicLLM, LLMProvider, OpenAILLM
from app.providers.mock_llm import MockLLM
from app.providers.tts import (
    ElevenLabsTTSProvider,
    MockTTSProvider,
    PollyTTSProvider,
    SayTTSProvider,
    TTSProvider,
)
from app.providers.vectors import PythonVectorStore, VectorStore


class ConfigError(RuntimeError):
    pass


@dataclass
class Providers:
    llm: LLMProvider
    embedder: EmbeddingProvider
    vectors: VectorStore
    tts: TTSProvider
    researcher_model: str
    writer_model: str
    small_model: str


@lru_cache
def default_embedder() -> EmbeddingProvider:
    cfg = pipeline_cfg().embeddings
    return make_embedder(cfg.provider, cfg.model, cfg.hashing_dim)


def make_llm(provider: str) -> LLMProvider:
    s, src, sched = get_settings(), pipeline_cfg().research.search, pipeline_cfg().scheduler
    if provider == "anthropic":
        # the Anthropic SDK reads ANTHROPIC_API_KEY from the environment itself
        return AnthropicLLM(src.anthropic_tool_type, None,
                            pipeline_cfg().research.max_pause_turns, sched.batch_poll_sec,
                            sched.batch_timeout_sec)
    if provider == "openai":
        return OpenAILLM(src.openai_tool_type, s.openai_api_key or None, sched.batch_poll_sec,
                         sched.batch_timeout_sec)
    if provider == "mock":
        return MockLLM()
    raise ConfigError(f"Unknown LLM_PROVIDER {provider!r}")


def make_tts(provider: str) -> TTSProvider:
    cfg = pipeline_cfg().tts
    if provider == "mock":
        return MockTTSProvider(cfg.mock_sample_rate)
    if provider == "say":
        return SayTTSProvider(cfg.voices, cfg.sample_rate)
    if provider == "polly":
        s = get_settings()
        return PollyTTSProvider(cfg.polly_engine, cfg.voices, s.aws_region,
                                s.aws_access_key_id, s.aws_secret_access_key,
                                max_concurrent=cfg.max_concurrent_requests)
    if provider == "elevenlabs":
        return ElevenLabsTTSProvider(get_settings().elevenlabs_api_key, cfg.elevenlabs_model,
                                     cfg.voices, max_concurrent=cfg.max_concurrent_requests)
    raise ConfigError(f"Unknown TTS provider {provider!r}")


def build_providers(
    mock: bool = False,
    llm: LLMProvider | None = None,
    tts: TTSProvider | None = None,
    embedder: EmbeddingProvider | None = None,
) -> Providers:
    """Assemble providers from settings. `mock=True` swaps LLM and TTS for offline fakes."""
    s = get_settings()
    llm = llm or make_llm("mock" if mock else s.llm_provider)
    tts = tts or make_tts("mock" if mock else pipeline_cfg().tts.provider)
    models = {"researcher": s.researcher_model, "writer": s.writer_model,
              "small": s.small_model}
    missing = [k for k, v in models.items() if not v]
    if missing:
        if llm.name != "mock":
            raise ConfigError(
                "Set " + ", ".join(f"{k.upper()}_MODEL" for k in missing) + " in .env"
            )
        models.update({k: f"mock-{k}" for k in missing})
    return Providers(
        llm=llm,
        embedder=embedder or default_embedder(),
        vectors=PythonVectorStore(),
        tts=tts,
        researcher_model=models["researcher"],
        writer_model=models["writer"],
        small_model=models["small"],
    )
