"""Database models: fields and relationships only.

Table names say what kind of table each one is:
  dim_*     things that are described (users, topics, stories, clips)
  fact_*    events that happen and grow over time (episodes, stories heard, costs)
  bridge_*  many-to-many links (which user follows which topic)
  ops_*     internal machinery (the job queue)
"""

from datetime import UTC, date, datetime
from typing import Any, Optional

from sqlalchemy import JSON, Column, Index, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


def utcnow() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(UTC)


def as_utc(dt: datetime) -> datetime:
    """Return `dt` as an aware UTC datetime (SQLite returns naive values)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


class User(SQLModel, table=True):
    """A listener. external_id is the login provider's user id (Clerk `sub`); empty for the
    dev user that exists when auth is off."""

    __tablename__ = "dim_user"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    external_id: str | None = Field(default=None, unique=True, index=True)
    # Secret in the user's private podcast feed URL (podcast apps can't sign in); reset to revoke
    feed_token: str | None = Field(default=None, unique=True, index=True)
    name: str = ""
    email: str = ""
    plan: str = Field(default="free", index=True)  # current plan (copy of the active
    #                                                  Subscription's, for fast lookups)
    created_at: datetime = Field(default_factory=utcnow)
    last_active_at: datetime = Field(default_factory=utcnow, index=True)

    profile: Optional["Profile"] = Relationship(back_populates="user")  # noqa: UP045
    episodes: list["Episode"] = Relationship(back_populates="user")
    subscriptions: list["Subscription"] = Relationship(back_populates="user")


class Subscription(SQLModel, table=True):
    """A period a user spent on one plan (config/plans.yaml). The user's history of plans;
    exactly one row per user is `active` (ended_at empty), and User.plan mirrors it.
    provider / provider_ref are for billing (e.g. a Stripe subscription id) once it exists."""

    __tablename__ = "fact_subscription"  # type: ignore[assignment]
    __table_args__ = (Index("ix_fact_subscription_user_status", "user_id", "status"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="dim_user.id")
    plan: str = Field(index=True)
    status: str = "active"  # active | ended
    price_usd_month: float = 0.0  # the plan's price when this period began
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None
    source: str = "signup"  # signup | self_serve | admin | cli | billing | migration
    provider: str = ""  # billing provider, empty while there is no billing
    provider_ref: str = ""  # the provider's subscription id

    user: User | None = Relationship(back_populates="subscriptions")


class Profile(SQLModel, table=True):
    """Podcast preferences for a user."""

    __tablename__ = "dim_profile"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="dim_user.id", unique=True)
    tone: str = "conversational"
    language: str = "en"
    voice: str = "aria"
    target_minutes: int = 10  # briefing length when no topic sets its own minutes
    delivery_hour: int = 7  # local hour at which the briefing is assembled
    timezone: str = "UTC"  # IANA name

    user: User | None = Relationship(back_populates="profile")



class Episode(SQLModel, table=True):
    """One user's episode. Each user owns their own rows. Audio lives at
    storage/audio/users/<user_id>/episodes/<episode_id>.mp3; only shared clips are reused."""

    __tablename__ = "fact_episode"  # type: ignore[assignment]
    __table_args__ = (Index("ix_fact_episode_user_published", "user_id", "published_at"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="dim_user.id", index=True)
    topic: str = Field(default="", index=True)
    title: str = ""
    description: str = ""
    image_url: str | None = None
    # Only the greeting / transitions / outro text (~1 KB). The full transcript and the
    # sources are rebuilt from fact_episode_story + the clip scripts (episode_transcript()).
    frame: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    audio_path: str | None = None
    duration_seconds: int | None = None
    audio_bytes: int | None = None  # file size: podcast feeds must state it (enclosure length)
    listened: bool = False
    listened_at: datetime | None = None
    # Playback, reported by the player: furthest second heard, first and last play
    progress_seconds: int = 0
    started_at: datetime | None = None
    last_played_at: datetime | None = Field(default=None, index=True)
    # Settings the episode was made with (the profile may change later)
    tone: str = ""
    language: str = ""
    voice: str = ""
    status: str = "pending"  # pending | generating | ready | failed
    # The accuracy review (pipeline/review.py): issues found, segments removed, rounds
    quality: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # Time to generate, end to end: the critical path of the work that built it (its topics'
    # research, then its stories' scripts and recordings, then assembly), without the planned
    # wait before delivery. timing = the seconds of each part (jobs/scheduler.py)
    ready_at: datetime | None = None
    generation_seconds: float | None = None
    timing: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    published_at: datetime = Field(default_factory=utcnow, index=True)  # = delivery time
    created_at: datetime = Field(default_factory=utcnow)

    user: User | None = Relationship(back_populates="episodes")


# ---------------------------------------------------------------------------
# Content pipeline (shared research + cached clips + per-user assembly)
# ---------------------------------------------------------------------------


class Voice(SQLModel, table=True):
    """A voice listeners can pick in Settings (Profile.voice stores its key). Loaded from
    config/voices.yaml on startup; `providers` maps it to a real voice per TTS provider."""

    __tablename__ = "dim_voice"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(unique=True, index=True)  # aria, leo, ... (never renamed)
    label: str
    style: str = ""  # "Warm and professional": what the voice is for
    gender: str = ""
    accent: str = ""  # of its English (default) voice
    accents: dict[str, str] = Field(default_factory=dict, sa_column=Column(JSON))  # per language
    sort_order: int = 0
    active: bool = True  # retired voices stay for old profiles but aren't offered
    # {"elevenlabs": "<voice id>", "polly": {"en": "Joanna", ...}, "say": ..., ...}
    providers: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # language -> sample in audio storage (voices/<provider>/<provider voice>/<lang>/<key>.mp3)
    previews: dict[str, str] = Field(default_factory=dict, sa_column=Column(JSON))
    updated_at: datetime = Field(default_factory=utcnow)


class Topic(SQLModel, table=True):
    """A shared research unit. Cost scales with the number of active topics, not users."""

    __tablename__ = "dim_topic"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(unique=True, index=True)
    display_name: str
    parent_id: int | None = Field(default=None, foreign_key="dim_topic.id", index=True)
    embedding: list[float] | None = Field(default=None, sa_column=Column(JSON))
    active_subscribers: int = Field(default=0, index=True)
    # Name shown to listeners per interface language ({"en": "Film", "es": "Cine", ...}),
    # from config/interests.yaml; empty for topics not offered in Settings
    labels: dict[str, str] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)


class Subtopic(SQLModel, table=True):
    """A subcategory of a topic that listeners can focus on, e.g. Film & TV > Cannes Film
    Festival. Shared: step 1 classifies a phrase once and every listener reuses the result.
    Stories of the topic are tagged with the subtopics they match (Story.subtopics)."""

    __tablename__ = "dim_subtopic"  # type: ignore[assignment]
    __table_args__ = (UniqueConstraint("topic_id", "key"),)

    id: int | None = Field(default=None, primary_key=True)
    topic_id: int = Field(foreign_key="dim_topic.id", index=True)
    key: str  # slug, unique within its topic
    label: str  # canonical name shown to listeners
    keywords: list[str] = Field(default_factory=list, sa_column=Column(JSON))  # match terms
    aliases: list[str] = Field(default_factory=list, sa_column=Column(JSON))  # phrases seen
    embedding: list[float] | None = Field(default=None, sa_column=Column(JSON))
    labels: dict[str, str] = Field(default_factory=dict, sa_column=Column(JSON))  # per language
    suggested: bool = False  # offered in Settings (config/subtopics.yaml); else listener-made
    sort_order: int = 0  # order among a topic's suggestions
    created_at: datetime = Field(default_factory=utcnow)


class TopicAlias(SQLModel, table=True):
    """A normalized user phrase that resolves to a topic (cached resolution decision)."""

    __tablename__ = "dim_topic_alias"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    topic_id: int = Field(foreign_key="dim_topic.id", index=True)
    alias_normalized: str = Field(unique=True, index=True)
    keyword: str | None = None  # niche phrase applied as a keyword filter on the topic
    source: str = "seed"  # seed | embedding | llm
    created_at: datetime = Field(default_factory=utcnow)


class UserTopic(SQLModel, table=True):
    """What a user follows: the single list shared by the Settings page and the pipeline.
    Empty keyword_filters means "all stories of the topic"."""

    __tablename__ = "bridge_user_topic"  # type: ignore[assignment]
    __table_args__ = (UniqueConstraint("user_id", "topic_id"),)

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="dim_user.id", index=True)
    topic_id: int = Field(foreign_key="dim_topic.id", index=True)
    interest: str = ""  # what the user picked or typed, e.g. "environment" -> topic climate
    frequency: str = "daily"  # daily | weekdays | weekly (see assemble.weekly_weekday)
    target_minutes: int | None = None  # this topic's share of the briefing; None = profile's
    weight: float = 1.0
    keyword_filters: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # Subtopic keys this listener focuses on: matching stories rank higher (not a filter)
    subtopics: list[str] = Field(default_factory=list, sa_column=Column(JSON))


class Story(SQLModel, table=True):
    """A researched news story (shared by every subscriber of its topic)."""

    __tablename__ = "dim_story"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    topic_id: int = Field(foreign_key="dim_topic.id", index=True)
    canonical_url: str = Field(unique=True, index=True)
    headline: str
    summary: str
    why_it_matters: str = ""
    source_name: str
    published_at: datetime = Field(index=True)
    importance_score: int = 5
    embedding: list[float] | None = Field(default=None, sa_column=Column(JSON))
    first_seen: datetime = Field(default_factory=utcnow, index=True)
    status: str = "active"  # active | update | retracted
    update_of_id: int | None = Field(default=None, foreign_key="dim_story.id")
    subtopics: list[str] = Field(default_factory=list, sa_column=Column(JSON))  # keys it matches
    # Research language: its text and main source are in this language (local-language research
    # finds e.g. El Pais for Spanish listeners); listeners get English stories + their own.
    language: str = Field(default="en", index=True)
    # Other outlets that reported it ([{"name", "url"}]): plural sourcing, cited in scripts
    sources: list[dict[str, str]] = Field(default_factory=list, sa_column=Column(JSON))
    # Context for terms a listener may not know, from Wikipedia ([{"term", "summary", "url",
    # "language"}]); scripts may use it to explain, never as news
    background: list[dict[str, str]] = Field(default_factory=list, sa_column=Column(JSON))


class StoryClip(SQLModel, table=True):
    """A spoken clip for one story in one (tone, language, voice): generated once, reused."""

    __tablename__ = "dim_story_clip"  # type: ignore[assignment]
    __table_args__ = (UniqueConstraint("story_id", "tone", "language", "voice"),)

    id: int | None = Field(default=None, primary_key=True)
    story_id: int = Field(foreign_key="dim_story.id", index=True)
    tone: str
    language: str
    voice: str
    script: str = ""
    audio_path: str | None = None
    duration_sec: float | None = None
    verified: bool = False
    verification_notes: str = ""
    cost_estimate: float = 0.0
    created_at: datetime = Field(default_factory=utcnow)


class PhraseClip(SQLModel, table=True):
    """A short spoken phrase shared by every listener with the same settings: the greeting
    (per time of day and first name), one transition per topic, and rotating outros.
    Written once by the small model, recorded once per voice, then reused."""

    __tablename__ = "dim_phrase_clip"  # type: ignore[assignment]
    __table_args__ = (UniqueConstraint("key", "tone", "language", "voice"),)

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True)  # greeting:morning:ana | transition:ai | outro:2
    tone: str
    language: str
    voice: str
    text: str
    audio_path: str | None = None
    duration_sec: float | None = None
    created_at: datetime = Field(default_factory=utcnow)


class EpisodeReview(SQLModel, table=True):
    """Cache of accuracy reviews (pipeline/review.py), keyed by the exact content reviewed:
    every listener whose episode reads the same segments shares one review."""

    __tablename__ = "dim_episode_review"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(unique=True, index=True)  # sha256 of the segments' ids, texts, language
    language: str = ""
    issues: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    model: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class EpisodeStory(SQLModel, table=True):
    """The user's story ledger: which stories went into which episode."""

    __tablename__ = "fact_episode_story"  # type: ignore[assignment]
    # The unique key also serves lookups by episode; (user_id, created_at) serves the
    # "heard in the last 14 days" check. Rows older than maintenance.heard_rows_keep_days
    # move to the archive (jobs/maintenance.py).
    __table_args__ = (UniqueConstraint("episode_id", "story_id"),
                      Index("ix_fact_episode_story_user_created", "user_id", "created_at"))

    id: int | None = Field(default=None, primary_key=True)
    episode_id: int = Field(foreign_key="fact_episode.id")
    story_id: int = Field(foreign_key="dim_story.id")
    position: int
    user_id: int = Field(foreign_key="dim_user.id")  # denormalized for fast lookups
    created_at: datetime = Field(default_factory=utcnow)


class CostEvent(SQLModel, table=True):
    """One billable action (LLM call, web searches, TTS). Feeds the metrics dashboard."""

    __tablename__ = "fact_cost_event"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    stage: str = Field(index=True)  # research | clip_write | clip_adapt | clip_verify | ...
    ref_id: str = Field(default="", index=True)  # e.g. topic:ai, story:12, episode:5
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    searches: int = 0
    tts_chars: int = 0
    batched: bool = False
    cost_usd: float = 0.0
    created_at: datetime = Field(default_factory=utcnow, index=True)


class KpiDaily(SQLModel, table=True):
    """Daily podcast KPIs per podcast language, voice and topic, aggregated by
    tracking/kpis.py from fact_episode, dim_user and dim_story_clip. Every measure adds up
    over days. topic "" = all users; a topic key = only the users who follow it (a user
    counts in each topic they follow, so never sum across topics)."""

    __tablename__ = "fact_kpi_daily"  # type: ignore[assignment]
    __table_args__ = (UniqueConstraint("day", "language", "voice", "topic"),)

    id: int | None = Field(default=None, primary_key=True)
    day: date = Field(index=True)
    language: str = ""
    voice: str = ""
    topic: str = ""  # Topic.key, "" = all
    new_users: int = 0  # signed up that day (their profile's language / voice)
    episodes_created: int = 0
    episodes_ready: int = 0
    episodes_failed: int = 0
    # Episodes delivered (ready, published) that day, and what became of them so far
    delivered: int = 0
    delivered_started: int = 0
    delivered_finished: int = 0
    delivered_seconds_heard: int = 0
    delivered_seconds_started: int = 0  # length of the started ones (completion rate)
    clips: int = 0  # verified story clips recorded (topic = the story's topic)


class ListenerDaily(SQLModel, table=True):
    """One row per user and day they listened: unique listeners, retention and stickiness
    need people, not sums. Aggregated by tracking/kpis.py from fact_episode."""

    __tablename__ = "fact_listener_daily"  # type: ignore[assignment]
    __table_args__ = (UniqueConstraint("day", "user_id", "language", "voice"),)

    id: int | None = Field(default=None, primary_key=True)
    day: date = Field(index=True)
    user_id: int = Field(foreign_key="dim_user.id", index=True)
    language: str = ""  # of the episodes heard
    voice: str = ""
    started: int = 0  # episodes first played that day
    finished: int = 0  # episodes finished that day
    seconds_heard: int = 0


class TopicDaily(SQLModel, table=True):
    """Per topic and day: followers (a snapshot taken that day; follows have no date, so
    past days can't be rebuilt and stay None) and stories researched."""

    __tablename__ = "fact_topic_daily"  # type: ignore[assignment]
    __table_args__ = (UniqueConstraint("day", "topic_id"),)

    id: int | None = Field(default=None, primary_key=True)
    day: date = Field(index=True)
    topic_id: int = Field(foreign_key="dim_topic.id", index=True)
    followers: int | None = None
    stories: int = 0


class CostDaily(SQLModel, table=True):
    """Daily cost summary per stage and model: what dashboards read (MoM, YTD). Rebuilt
    from fact_cost_event, whose raw rows are kept for maintenance.cost_events_keep_days."""

    __tablename__ = "fact_cost_daily"  # type: ignore[assignment]
    __table_args__ = (UniqueConstraint("day", "stage", "model"),)

    id: int | None = Field(default=None, primary_key=True)
    day: date = Field(index=True)
    stage: str
    model: str
    events: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    searches: int = 0
    tts_chars: int = 0
    cost_usd: float = 0.0


class Job(SQLModel, table=True):
    """DB-backed queue entry."""

    __tablename__ = "ops_job"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    type: str = Field(index=True)  # research | clip | assemble
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    idempotency_key: str = Field(unique=True, index=True)
    status: str = Field(default="queued", index=True)  # queued | running | done | dead
    attempts: int = 0
    max_attempts: int = 4
    run_after: datetime = Field(default_factory=utcnow, index=True)
    locked_at: datetime | None = None
    locked_by: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
