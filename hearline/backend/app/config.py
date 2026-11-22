"""All configuration in one place.

- Settings: secrets, model names and provider choices from the environment / .env.
- *_cfg(): typed, cached access to config/*.yaml and prompts/tones.yaml (tunable knobs).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import AliasChoices, BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BACKEND_DIR / "config"
PROMPTS_DIR = BACKEND_DIR / "prompts"


class Settings(BaseSettings):
    """Runtime configuration (secrets, model names, provider choices). See .env.example.

    Tunable pipeline parameters live in config/pipeline.yaml (see the *_cfg() loaders below).
    """

    model_config = SettingsConfigDict(
        # repo root, hearline/, backend/ (the last one found wins)
        env_file=(BACKEND_DIR.parent.parent / ".env", BACKEND_DIR.parent / ".env",
                  BACKEND_DIR / ".env"),
        extra="ignore",
    )

    openai_api_key: str = ""
    elevenlabs_api_key: str = ""
    # Amazon Polly (optional: boto3 also reads ~/.aws or an IAM role)
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-1"
    # Where data lives. Relative paths are resolved from hearline/backend/, not the current folder.
    #  1. Records: users, profiles, topics, stories, episode details, costs, jobs.
    database_url: str = "sqlite:///storage/hearline.db"
    #  2. Audio files: shared story clips (clips/) and each user's episodes
    #     (users/<user_id>/episodes/<episode_id>.mp3). Records store paths relative to this.
    audio_dir: Path = BACKEND_DIR / "storage" / "audio"
    #     In production AUDIO_STORAGE=s3 publishes them to AUDIO_BUCKET (AUDIO_DIR is then a
    #     local cache) and the API hands out expiring signed links (app/providers/storage.py).
    audio_storage: str = "local"  # local | s3
    audio_bucket: str = ""
    audio_prefix: str = ""
    audio_url_ttl_sec: int = 86400
    cloudfront_domain: str = ""  # optional: serve through CloudFront (signed URLs)
    cloudfront_key_id: str = ""
    cloudfront_private_key: str = ""  # PEM of the CloudFront key group's private key
    # Sent to Wikipedia and news sites when research checks links / fetches background.
    # Wikimedia asks for contact info: set e.g. "Hearline/1.0 (https://your.site; you@site)"
    http_user_agent: str = "HearlineBot/0.1 (news podcast research)"
    # Where listeners reach the API from outside (podcast feed links must be absolute), e.g.
    # https://hearline.app; podcast cover art (square, 1400-3000 px JPEG/PNG; optional)
    public_base_url: str = "http://localhost:8000"
    podcast_image_url: str = ""
    # Alerts (app/tracking/alerts.py): Slack / Teams / Discord incoming-webhook URL, optional
    alert_webhook_url: str = ""
    alert_cooldown_minutes: int = 60  # same alert kind at most once per this, per process
    #  3. Archive: old fact rows as dated JSON Lines files, for analytics (the future data lake)
    archive_dir: Path = BACKEND_DIR / "storage" / "archive"
    cors_origins: list[str] = ["http://localhost:5173"]
    # production refuses to start with dev-only settings (no login, files served unchecked)
    app_env: str = "dev"  # dev | production
    # Login (app/api/auth.py). dev: X-User-Id header, no login. clerk: verify Clerk tokens.
    auth_mode: str = "dev"  # dev | clerk
    clerk_issuer: str = ""  # e.g. https://your-app.clerk.accounts.dev (Clerk "Frontend API URL")
    clerk_authorized_parties: list[str] = []  # frontend origins, e.g. ["https://hearline.app"]
    # Admin dashboard: Clerk user ids allowed in, e.g. ["user_2abc"] (everyone in dev mode)
    admin_user_ids: list[str] = []
    # Clerk webhook signing secret ("whsec_..."): name/email sync and account erasure
    clerk_webhook_secret: str = ""
    # Plans (config/plans.yaml): there is no billing yet, so in production only admins change
    # a user's plan; set PLAN_SELF_SERVE=true to let users switch themselves (always on in dev)
    plan_self_serve: bool = False

    # Content pipeline: providers and models (no defaults in code; set them in .env).
    llm_provider: str = "anthropic"  # anthropic | openai | mock
    researcher_model: str = ""
    writer_model: str = ""
    # Small, cheap model: fact-checking, topic matching, spoken phrases. VERIFIER_MODEL is
    # the old name and still works.
    small_model: str = Field("", validation_alias=AliasChoices("SMALL_MODEL", "VERIFIER_MODEL"))
    # Optional overrides of config/pipeline.yaml
    tts_provider: str = ""  # mock | say | polly | elevenlabs
    embedding_provider: str = ""  # sentence-transformers | hashing
    daily_budget_usd: float | None = None


    @model_validator(mode="after")
    def _anchor_paths(self) -> Settings:
        """Resolve relative storage paths against the backend folder, from any working directory."""
        prefix = "sqlite:///"
        db_path = self.database_url.removeprefix(prefix)
        if self.database_url.startswith(prefix) and db_path and not db_path.startswith(("/", ":")):
            self.database_url = prefix + str((BACKEND_DIR / db_path).resolve())
        if not self.audio_dir.is_absolute():
            self.audio_dir = (BACKEND_DIR / self.audio_dir).resolve()
        if not self.archive_dir.is_absolute():
            self.archive_dir = (BACKEND_DIR / self.archive_dir).resolve()
        if self.app_env == "production":
            unsafe = [f"{k}={v}" for k, v, ok in (
                ("AUTH_MODE", self.auth_mode, "clerk"),
                ("AUDIO_STORAGE", self.audio_storage, "s3")) if v != ok]
            if self.database_url.startswith("sqlite"):
                unsafe.append("DATABASE_URL=sqlite")
            # offline fakes: a mock LLM writes canned scripts, a mock voice publishes test tones.
            # The voice must be set explicitly, because config/pipeline.yaml defaults to mock.
            if self.llm_provider == "mock":
                unsafe.append("LLM_PROVIDER=mock")
            if self.tts_provider in ("", "mock"):
                unsafe.append(f"TTS_PROVIDER={self.tts_provider or '(unset: pipeline.yaml mock)'}")
            if unsafe:
                raise ValueError("APP_ENV=production refuses dev settings: " + ", ".join(unsafe))
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the cached Settings instance."""
    return Settings()


# --- YAML config (config/*.yaml, prompts/) ---------------------------------------
class StageLLM(BaseModel):
    """Per-stage LLM request knobs (the model name itself comes from the environment)."""

    max_tokens: int = 4000
    effort: str | None = None  # Anthropic output_config.effort / OpenAI reasoning.effort
    refusal_fallbacks: str | None = None  # Anthropic server-side fallbacks mode, e.g. "default"


class TopicsCfg(BaseModel):
    match_threshold: float = 0.75
    llm_candidates: int = 5
    niche_as_keyword: bool = True
    research_keyword_hints: int = 10
    max_subtopics: int = 5  # focus subcategories a listener can add per topic
    max_new_phrases_per_day: int = 20  # LLM classifications per listener per UTC day


class SearchCfg(BaseModel):
    anthropic_tool_type: str = "web_search_20250305"
    openai_tool_type: str = "web_search"
    max_uses: int = 5
    allowed_domains: list[str] = Field(default_factory=list)
    blocked_domains: list[str] = Field(default_factory=list)


class ResearchLanguage(BaseModel):
    """Local-language research: where to look for a language's own news."""

    regions: str = ""  # e.g. "Spain and Latin America"
    outlets: list[str] = Field(default_factory=list)  # reputable outlets to prefer (a mix)


class ResearchCfg(BaseModel):
    # Each topic is researched in English, plus in every listed language its active listeners
    # use (cost grows with topics x languages in use, never with users). Empty = English only.
    languages: dict[str, ResearchLanguage] = Field(default_factory=dict)
    english_outlets: list[str] = Field(default_factory=list)
    max_per_source: int = 2  # stories from one outlet per research call (plural sourcing)
    max_other_sources: int = 3  # extra outlets kept per story
    verify_urls: bool = True  # article links only (no section/tag pages), and not dead (404)
    relevance_check: bool = True  # one small call per research run drops off-topic stories
    # Ranking weight per outlet domain (step 4): >1 trusted, <1 demoted, unlisted = 1.0
    source_weights: dict[str, float] = Field(default_factory=dict)
    background_terms: int = 2  # Wikipedia context per story for terms listeners may not know
    lookback_hours: int = 24
    min_stories: int = 5
    max_stories: int = 8
    dedupe_window_days: int = 3
    headline_dup_threshold: float = 0.85
    update_min_hours: int = 6
    max_attempts: int = 3
    backoff_base_sec: float = 2.0
    max_pause_turns: int = 3
    llm: StageLLM = StageLLM(max_tokens=16000, effort="medium")
    search: SearchCfg = SearchCfg()


class ClipsCfg(BaseModel):
    min_seconds: int = 20
    max_seconds: int = 60
    words_per_minute: dict[str, int] = Field(default_factory=lambda: {"default": 150})
    length_tolerance: float = 0.15
    # base_adapt: one script per story from the record, other tones/languages adapted from it
    # by the small model (cheapest). per_combo: every (tone, language) from the record.
    strategy: str = "base_adapt"
    writer: StageLLM = StageLLM(max_tokens=4000, effort="low")
    adapter: StageLLM = StageLLM(max_tokens=4000, effort="low")
    verifier: StageLLM = StageLLM(max_tokens=2000, effort="low")
    # A rejected script gets an independent second check by WRITER_MODEL before a rewrite
    second_opinion: bool = True


class ReviewCfg(BaseModel):
    enabled: bool = True
    max_tokens: int = 3000
    effort: str = "medium"
    rounds: int = 2  # re-plan at most this many times after removing flagged segments
    redundant_sentence_share: float = 0.7  # word overlap that makes two sentences one


class AssembleCfg(BaseModel):
    lookback_hours: int = 48
    weekly_lookback_hours: int = 168  # weekly topics pick from the whole week's stories
    weekly_weekday: int = 0  # weekly topics play on Mondays (0 = Monday ... 6 = Sunday)
    max_episode_minutes: int = 30  # cap on the sum of the day's topic minutes
    seen_window_days: int = 14
    recency_half_life_hours: float = 24.0
    max_per_topic: int = 12  # safety cap; each topic's own minutes do the real limiting
    min_importance: int = 4  # stories the researcher scored below this (1-10) are never used
    default_clip_seconds: float = 40.0
    frame_overhead_seconds: float = 25.0
    transition_seconds: float = 4.0
    max_refill_rounds: int = 3
    gap_ms: int = 600
    crossfade_ms: int = 150
    target_dbfs: float = -16.0
    sfx_offset_db: float = -8.0  # sounds/ clips sit this far under the voice
    sfx_offsets_db: dict[str, float] = Field(default_factory=lambda: {"transition": -3.0})
    topic_pause_ms: int = 700  # extra silence before the topic-change sound
    export_format: str = "mp3"
    mp3_bitrate: str = "64k"  # mono speech: 64 kbps is podcast quality, 1/3 smaller than 96
    phrase_llm: StageLLM = StageLLM(max_tokens=2000, effort="low")
    outro_variants: int = 3  # outros rotate by day; each is written and recorded once
    teasers: int = 3  # "coming up" rundown: one shared teaser per top story (0 = no rundown)
    subtopic_boost: float = 1.0  # stories in a listener's subcategories score (1 + boost)x
    local_language_boost: float = 0.5  # stories researched in the listener's language: 1.5x
    # Slow news days: daily topics also use unheard stories up to this old (recency decay
    # keeps today's first), then up to max_explainers Wikipedia explainers; else it's shorter
    backfill_hours: int = 96
    max_explainers: int = 2
    explainer_score: float = 0.01  # explainers rank after every news story
    # Same subject, different headline (e.g. two stories about the new James Bond): stories
    # sharing at least this many names, and this share of the shorter list, plus this headline
    # similarity, are one subject; the episode keeps the better one.
    # Accuracy review of the whole episode before recording (pipeline/review.py)
    review: ReviewCfg = Field(default_factory=lambda: ReviewCfg())
    same_subject_min_names: int = 2
    same_subject_name_share: float = 0.6
    same_subject_min_similarity: float = 0.45


class SchedulerCfg(BaseModel):
    inactive_days: int = 14
    default_delivery_hour: int = 7
    poll_interval_sec: float = 2.0
    lock_timeout_sec: int = 1800
    max_attempts: int = 4
    backoff_base_sec: float = 30.0
    concurrency: dict[str, int] = Field(
        default_factory=lambda: {"research": 4, "clip": 8, "assemble": 4, "maintenance": 1}
    )
    daily_budget_usd: float = 25.0
    use_batches: bool = False
    batch_max_requests: int = 500
    batch_poll_sec: float = 30.0
    batch_timeout_sec: int = 86400
    clip_prefetch_top_k: int | None = 4
    # Episodes are built ahead of delivery, spread over this window, so a timezone's 7:00 is
    # not one big spike. They become visible at the delivery time.
    build_lead_hours: float = 3.0
    # An episode waits for its own topics' research, but stops waiting this many minutes
    # before delivery and goes ahead with the stories it has.
    research_wait_margin_minutes: int = 15
    insert_chunk_size: int = 1000  # rows per bulk INSERT in the daily plan
    # The worker runs the daily plan this often (idempotent): each run queues every user's
    # next local delivery, so all timezones are covered without a separate cron job.
    plan_every_minutes: int = 60
    worker_threads: int = 4  # jobs one worker process runs in parallel (network-bound work)


class MaintenanceCfg(BaseModel):
    """Daily clean-up (jobs/maintenance.py): keeps the fast-growing tables small."""

    heard_rows_keep_days: int = 30  # fact_episode_story; older rows go to the archive
    cost_events_keep_days: int = 90  # fact_cost_event; fact_cost_daily keeps the totals
    done_jobs_keep_days: int = 7
    dead_jobs_keep_days: int = 30


class EmbeddingsCfg(BaseModel):
    provider: str = "sentence-transformers"
    model: str = "sentence-transformers/all-MiniLM-L6-v2"
    hashing_dim: int = 256


class TTSCfg(BaseModel):
    provider: str = "mock"
    sample_rate: int = 22050
    mock_sample_rate: int = 8000
    polly_engine: str = "neural"  # standard | neural | generative | long-form
    elevenlabs_model: str = "eleven_flash_v2_5"
    # Simultaneous voice requests per worker process (0 = no limit). ElevenLabs caps them per
    # plan; extra ones are queued or refused (429). Set to your plan's limit / worker tasks.
    max_concurrent_requests: int = 4
    voices: dict[str, dict[str, Any]] = Field(default_factory=dict)


class VoiceLanguage(BaseModel):
    """A voice's native speaker in one language (ElevenLabs Voice Library)."""

    elevenlabs: str = ""
    accent: str = ""  # shown in Settings for this language, e.g. "Spain", "Quebec"
    locale: str = ""  # e.g. es-ES, fr-CA (for the record)


class VoiceEntry(BaseModel):
    """One voice in config/voices.yaml (see that file). The top-level elevenlabs/accent are
    its English (default) voice; `languages` gives a native speaker per other language."""

    key: str
    label: str
    style: str = ""
    gender: str = ""
    accent: str = ""
    active: bool = True
    elevenlabs: str = ""
    polly: dict[str, str] = Field(default_factory=dict)
    say: str = ""
    say_by_language: dict[str, str] = Field(default_factory=dict)
    languages: dict[str, VoiceLanguage] = Field(default_factory=dict)

    def providers(self) -> dict[str, Any]:
        """The provider mapping the TTS providers read (tts.voices[key])."""
        return {"elevenlabs": self.elevenlabs, "polly": self.polly, "say": self.say,
                "say_by_language": self.say_by_language,
                "elevenlabs_by_language": {k: v.elevenlabs for k, v in self.languages.items()
                                           if v.elevenlabs}}

    def accents(self) -> dict[str, str]:
        """Accent per language as Settings shows it ("en" = the top-level accent)."""
        return {"en": self.accent, **{k: v.accent for k, v in self.languages.items()
                                      if v.accent}}


class VoicesCfg(BaseModel):
    preview_text: dict[str, str] = Field(default_factory=dict)  # language -> sample sentence
    voices: list[VoiceEntry]


class PipelineCfg(BaseModel):
    topics: TopicsCfg = TopicsCfg()
    research: ResearchCfg = ResearchCfg()
    clips: ClipsCfg = ClipsCfg()
    assemble: AssembleCfg = AssembleCfg()
    scheduler: SchedulerCfg = SchedulerCfg()
    embeddings: EmbeddingsCfg = EmbeddingsCfg()
    tts: TTSCfg = TTSCfg()
    maintenance: MaintenanceCfg = MaintenanceCfg()


class ModelPrice(BaseModel):
    input: float
    output: float
    cache_write: float | None = None
    cache_read: float | None = None


class PricingCfg(BaseModel):
    """USD prices. LLM prices are per 1M tokens; TTS per 1M characters."""

    llm: dict[str, ModelPrice] = Field(default_factory=dict)
    web_search_per_1k: dict[str, float] = Field(default_factory=dict)
    batch_discount: float = 0.5
    tts_per_1m_chars: dict[str, float] = Field(default_factory=dict)


class PlanCfg(BaseModel):
    """One subscription plan (config/plans.yaml). "all" = no restriction."""

    key: str = ""
    label: str
    price_usd_month: float = 0.0
    max_topics: int | None = None
    max_topic_minutes: int
    max_episode_minutes: int
    subtopics: bool = True
    voices: list[str] | Literal["all"] = "all"
    tones: list[str] | Literal["all"] = "all"

    def allows_voice(self, voice: str) -> bool:
        return self.voices == "all" or voice in self.voices

    def allows_tone(self, tone: str) -> bool:
        return self.tones == "all" or tone in self.tones


class PlansCfg(BaseModel):
    default: str
    plans: dict[str, PlanCfg]

    def get(self, key: str | None) -> PlanCfg:
        """The plan `key`, or the default plan if it is unknown (e.g. a removed plan)."""
        return self.plans.get(key or "", self.plans[self.default])


class TonePreset(BaseModel):
    label: str
    instructions: str


class TonesCfg(BaseModel):
    presets: dict[str, TonePreset]
    aliases: dict[str, str] = Field(default_factory=dict)
    default: str = "conversational"

    def preset_for(self, tone: str) -> str:
        """Map any user tone onto one of the (few) cached presets."""
        if tone in self.presets:
            return tone
        return self.aliases.get(tone, self.default)


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@lru_cache
def voices_cfg() -> VoicesCfg:
    return VoicesCfg.model_validate(_load(CONFIG_DIR / "voices.yaml"))


@lru_cache
def pipeline_cfg() -> PipelineCfg:
    cfg = PipelineCfg.model_validate(_load(CONFIG_DIR / "pipeline.yaml"))
    cfg.tts.voices = {v.key: v.providers() for v in voices_cfg().voices}  # config/voices.yaml
    s = get_settings()
    if s.tts_provider:
        cfg.tts.provider = s.tts_provider
    if s.embedding_provider:
        cfg.embeddings.provider = s.embedding_provider
    if s.daily_budget_usd is not None:
        cfg.scheduler.daily_budget_usd = s.daily_budget_usd
    return cfg


@lru_cache
def plans_cfg() -> PlansCfg:
    cfg = PlansCfg.model_validate(_load(CONFIG_DIR / "plans.yaml"))
    for key, plan in cfg.plans.items():
        plan.key = key
    return cfg


@lru_cache
def pricing_cfg() -> PricingCfg:
    return PricingCfg.model_validate(_load(CONFIG_DIR / "pricing.yaml"))


@lru_cache
def tones_cfg() -> TonesCfg:
    return TonesCfg.model_validate(_load(PROMPTS_DIR / "tones.yaml"))


@lru_cache
def interests_cfg() -> list[dict[str, str]]:
    """The interest catalog shown in Settings (slug, label, image)."""
    return yaml.safe_load((CONFIG_DIR / "interests.yaml").read_text(encoding="utf-8")) or []


def interest_images() -> dict[str, str]:
    """Interest slug -> its catalog image (also used as the episode image)."""
    return {i["slug"]: i.get("image", "") for i in interests_cfg()}


def interest_labels() -> dict[str, str]:
    """Interest slug -> its English label (topic episode titles)."""
    return {i["slug"]: i["label"] for i in interests_cfg()}


@lru_cache
def default_profile_cfg() -> dict[str, Any]:
    """Tone, language, voice and topics a new user starts with."""
    return _load(CONFIG_DIR / "default_profile.yaml")


@lru_cache
def ui_options() -> dict[str, list[dict[str, str]]]:
    """Tone and language choices for Settings: tones are the presets of prompts/tones.yaml
    (aliases are accepted from old profiles, never offered), languages from
    config/options.yaml. `value` is what a profile stores."""
    tones = tones_cfg()
    return {"tones": [{"value": k, "label": p.label} for k, p in tones.presets.items()],
            "languages": _load(CONFIG_DIR / "options.yaml").get("languages", [])}


def language_names() -> dict[str, str]:
    """Language code -> display name."""
    return {o["value"]: o["label"] for o in ui_options()["languages"]}


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")
