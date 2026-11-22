"""API endpoints. Who is asking: current_user() (Clerk login, or the dev header locally)."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, StringConstraints
from sqlmodel import Session, col, func, select

from app.accounts import delete_account
from app.api.auth import AuthError, verify_claims
from app.config import default_profile_cfg, get_settings, interests_cfg, plans_cfg, ui_options
from app.database.db import get_session
from app.database.models import (
    Episode,
    Profile,
    Subtopic,
    Topic,
    TopicAlias,
    User,
    UserTopic,
    Voice,
    as_utc,
    utcnow,
)
from app.pipeline.step1_topics import (
    PhraseLimitError,
    SubtopicError,
    normalize,
    set_interests,
    suggested_subtopics,
)
from app.plans import (
    fit_to_plan,
    plan_error,
    plan_of,
    plans_for_ui,
    set_plan,
    start_subscription,
)
from app.providers.storage import get_storage
from app.providers.voices import active_voices

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")
SessionDep = Annotated[Session, Depends(get_session)]

TOPIC_LABELS = {i["slug"]: i["label"] for i in interests_cfg()}
TOPIC_TRANSLATIONS = {i["slug"]: {"en": i["label"], **(i.get("labels") or {})}
                      for i in interests_cfg()}  # also stored as dim_topic.labels
STYLE_FIELDS = ("tone", "language")  # voices live in the database (dim_voice)
VALID = {f: {o["value"] for o in ui_options()[f"{f}s"]} for f in STYLE_FIELDS}


class TopicPref(BaseModel):
    """Per-topic preferences. subtopics: focus subcategories (e.g. "Cannes Festival" under
    Film); sent as typed, returned as their canonical label after step 1 classifies them."""

    topic: str
    frequency: Literal["daily", "weekdays", "weekly"]
    target_minutes: int
    subtopics: list[Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)]] = (
        Field(default_factory=list, max_length=10))
    # returned only: each subcategory's name per language ({"Cannes Film Festival": {"es": ...}})
    subtopic_labels: dict[str, dict[str, str]] = Field(default_factory=dict)


class ProfileIO(BaseModel):
    """Account + preferences as sent to / received from the UI."""

    name: str
    email: str
    tone: str
    language: str
    voice: str
    topics: list[TopicPref]
    plan: str = ""  # returned only (config/plans.yaml key); change it with PUT /api/plan


class EpisodeOut(BaseModel):
    """Episode as shown in the UI (no script)."""

    id: int
    topic: str
    topic_label: str
    topic_labels: dict[str, str] = {}  # topic name per interface language ({} for "briefing")
    title: str
    description: str
    image_url: str | None
    audio_url: str | None
    duration_seconds: int | None
    listened: bool
    progress_seconds: int = 0  # furthest point heard, to resume there
    status: str
    published_at: datetime


class ProgressIn(BaseModel):
    seconds: int = Field(ge=0)


def get_user(session: Session, user_id: int | None = None) -> User:
    """Dev mode: the user (the first one if no id), with a default profile on first use."""
    user = session.get(User, user_id) if user_id else session.exec(select(User)).first()
    if user is None:
        if user_id:
            raise HTTPException(status_code=404, detail="User not found.")
        user = User(name="Listener")
        session.add(user)
        session.commit()
        start_subscription(session, user, plans_cfg().default, "signup")
    return ensure_profile(session, user)


def login_user(session: Session, external_id: str, name: str | None = None,
               email: str | None = None) -> User:
    """The user behind a verified login, created (with the default profile) on first visit.
    With Clerk the login account owns the name and email: given here (token claims) or by the
    Clerk webhook, they overwrite what the app had."""
    user = session.exec(select(User).where(User.external_id == external_id)).first()
    if user is None:
        user = User(external_id=external_id)
        session.add(user)
        session.commit()
        start_subscription(session, user, plans_cfg().default, "signup")
    sync_identity(session, user, name, email)
    return ensure_profile(session, user)


def sync_identity(session: Session, user: User, name: str | None, email: str | None) -> None:
    """Copy the login account's name / email onto the user when they changed."""
    changed = False
    if name and name.strip() and user.name != name.strip():
        user.name, changed = name.strip(), True
    if email and email.strip() and user.email != email.strip():
        user.email, changed = email.strip(), True
    if changed:
        session.add(user)
        session.commit()


def ensure_profile(session: Session, user: User) -> User:
    """Give a new user the default profile and topics (config/default_profile.yaml)."""
    if user.profile is None:
        defaults = dict(default_profile_cfg())  # a copy: the loader is cached
        topics = defaults.pop("topics")
        session.add(Profile(user_id=user.id, **defaults))
        session.commit()
        set_interests(session, user.id,  # type: ignore[arg-type]
                      [(t["topic"], t["frequency"], t["target_minutes"]) for t in topics])
        session.refresh(user)
        fit_to_plan(session, user)  # the default profile may ask for more than the plan has
        start_pipeline(session, user, first_episode=True)  # first login: first episode now
    return user


def start_pipeline(session: Session, user: User, first_episode: bool = False) -> None:
    """Queue today's research for the user's topics (and, on first login, their first
    episode) so the worker starts now, not at the next hourly plan. Never fails a request."""
    from app.jobs.scheduler import kickoff_user  # the API only queues jobs; the worker runs them

    try:
        kickoff_user(session, user.id, first_episode=first_episode)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - the hourly plan catches up anyway
        session.rollback()
        log.exception("Could not start the pipeline for user %s", user.id)


def current_user(
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
    x_user_id: Annotated[int | None, Header()] = None,
) -> User:
    """Who the request is for. AUTH_MODE=clerk: the user of a valid Clerk token (401 if
    missing or invalid). AUTH_MODE=dev: the X-User-Id header, else the single default user.
    Also marks the user active: the daily plan skips users inactive for 14 days."""
    if get_settings().auth_mode == "clerk":
        token = (authorization or "").removeprefix("Bearer ").strip()
        try:
            claims = verify_claims(token) if token else None
            user = login_user(session, str(claims["sub"]), claims.get("name"),
                              claims.get("email")) if claims else None
        except AuthError:
            user = None
        if user is None:
            raise HTTPException(status_code=401, detail="Sign in required.",
                                headers={"WWW-Authenticate": "Bearer"})
    else:
        user = get_user(session, x_user_id)
    if utcnow() - as_utc(user.last_active_at) > timedelta(hours=1):  # one write per hour
        user.last_active_at = utcnow()
        session.add(user)
        session.commit()
    return user


UserDep = Annotated[User, Depends(current_user)]


class DevLoginIO(BaseModel):
    email: str
    name: str = ""
    create: bool = False  # the demo tool (app/dev/demo.py): create the user if new


@router.post("/dev/login")
def dev_login(data: DevLoginIO, session: SessionDep) -> dict[str, Any]:
    """Local development only (AUTH_MODE=dev): sign in as the user with this email, no password.
    The frontend then sends X-User-Id. A new user (create=True) gets the default profile and
    their first episode queued, like a real first login. With Clerk this endpoint is gone (404),
    and APP_ENV=production refuses dev login altogether."""
    if get_settings().auth_mode != "dev":
        raise HTTPException(status_code=404, detail="Not found.")
    email = data.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=422, detail="Enter an email address.")
    user = session.exec(select(User).where(func.lower(User.email) == email)).first()
    if user is None:
        if not data.create:
            raise HTTPException(status_code=404, detail="No user with this email.")
        user = User(name=data.name.strip() or email.split("@")[0].capitalize(), email=email)
        session.add(user)
        session.commit()
        start_subscription(session, user, plans_cfg().default, "signup")
    user = ensure_profile(session, user)
    return {"user_id": user.id, "name": user.name, "email": user.email}


def to_profile_io(session: Session, user: User) -> ProfileIO:
    """Combine user, profile and followed topics into the API shape."""
    profile = user.profile
    subs = session.exec(select(UserTopic).where(UserTopic.user_id == user.id)
                        .order_by(col(UserTopic.id))).all()
    rows = session.exec(select(Subtopic).where(
        col(Subtopic.topic_id).in_([ut.topic_id for ut in subs]))).all()
    labels = {(s.topic_id, s.key): s.label for s in rows}
    translations = {(s.topic_id, s.key): s.labels or {} for s in rows}
    return ProfileIO(
        name=user.name,
        email=user.email,
        tone=profile.tone,
        language=profile.language,
        voice=profile.voice,
        plan=plan_of(user).key,
        topics=[TopicPref(topic=ut.interest, frequency=ut.frequency,  # type: ignore[arg-type]
                          target_minutes=ut.target_minutes or profile.target_minutes,
                          subtopics=[labels[(ut.topic_id, k)] for k in (ut.subtopics or [])
                                     if (ut.topic_id, k) in labels],
                          subtopic_labels={labels[(ut.topic_id, k)]: translations[(ut.topic_id, k)]
                                           for k in (ut.subtopics or [])
                                           if (ut.topic_id, k) in labels})
                for ut in subs],
    )


def voice_option(voice: Voice) -> dict[str, Any]:
    """A voice as Settings shows it, with a playable sample link per recorded language."""
    storage = get_storage()
    return {"value": voice.key, "label": voice.label, "description": voice.style,
            "gender": voice.gender, "accent": voice.accent, "accents": voice.accents or {},
            "previews": {lang: storage.url(path) for lang, path in (voice.previews or {}).items()}}


@router.get("/options")
def get_options(session: SessionDep) -> dict[str, list[dict[str, Any]]]:
    """Choices for tone (prompts/tones.yaml), language (config/options.yaml), voice (dim_voice)."""
    return {**ui_options(), "voices": [voice_option(v) for v in active_voices(session)]}


@router.get("/interests")
def list_interests(session: SessionDep) -> list[dict[str, Any]]:
    """Catalog of selectable topics (slug, label, image) with each topic's suggested
    subcategories (config/subtopics.yaml), shown as one-tap chips in Settings."""
    suggestions = suggested_subtopics(session)
    out = []
    for item in interests_cfg():
        alias = session.exec(select(TopicAlias).where(
            TopicAlias.alias_normalized == normalize(item["slug"]))).first()
        topic = session.get(Topic, alias.topic_id) if alias else None
        out.append({"slug": item["slug"], "label": item["label"], "image": item.get("image", ""),
                    "labels": (topic.labels if topic else None) or {"en": item["label"]},
                    "suggestions": suggestions.get(topic.id, []) if topic else []})  # type: ignore[arg-type]
    return out


@router.get("/profile", response_model=ProfileIO)
def get_profile(session: SessionDep, user: UserDep) -> ProfileIO:
    """Return the current user's account and per-topic preferences."""
    return to_profile_io(session, user)


@router.put("/profile", response_model=ProfileIO)
def update_profile(data: ProfileIO, session: SessionDep, user: UserDep) -> ProfileIO:
    """Update the current user's account and replace their topic preferences."""
    slugs = [t.topic for t in data.topics]
    if not set(slugs) <= TOPIC_LABELS.keys() or len(set(slugs)) != len(slugs):
        raise HTTPException(status_code=422, detail="Unknown or duplicate topic.")
    for field, valid in VALID.items():
        if getattr(data, field) not in valid:
            raise HTTPException(status_code=422, detail=f"Unknown {field}.")
    if data.voice not in {v.key for v in active_voices(session)}:
        raise HTTPException(status_code=422, detail="Unknown voice.")
    error = plan_error(plan_of(user), data.tone, data.voice,
                       [(t.target_minutes, t.subtopics) for t in data.topics])
    if error:
        raise HTTPException(status_code=403, detail=error)
    # Step 1 classifies each topic and its subcategories first: a subcategory that doesn't
    # fit its topic rejects the whole save, so nothing is half-applied.
    try:
        set_interests(session, user.id,  # type: ignore[arg-type]
                      [(t.topic, t.frequency, t.target_minutes, t.subtopics)
                       for t in data.topics])
    except PhraseLimitError as exc:
        session.rollback()
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except SubtopicError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if get_settings().auth_mode != "clerk":   # with Clerk, the login account owns these
        user.name, user.email = data.name, data.email
    user.profile.tone, user.profile.language, user.profile.voice = (
        data.tone,
        data.language,
        data.voice,
    )
    session.add(user)
    session.commit()
    start_pipeline(session, user)  # research for new topics / language, today
    session.refresh(user)
    return to_profile_io(session, user)


class PlanIn(BaseModel):
    plan: str


@router.get("/plans")
def list_plans() -> dict[str, Any]:
    """Every plan with its limits and price, and whether users may switch plan themselves."""
    s = get_settings()
    return {"plans": plans_for_ui(), "self_serve": s.app_env == "dev" or s.plan_self_serve}


@router.put("/plan", response_model=ProfileIO)
def change_plan(data: PlanIn, session: SessionDep, user: UserDep) -> ProfileIO:
    """Switch the current user's plan (only while there is no billing and self-serve is on).
    A downgrade trims their Settings to fit the new plan."""
    if not list_plans()["self_serve"]:
        raise HTTPException(status_code=403, detail="Plan changes are not open yet.")
    try:
        set_plan(session, user, data.plan, "self_serve")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return to_profile_io(session, user)


def to_out(ep: Episode) -> EpisodeOut:
    """Convert an Episode row to its API shape (SQLite drops tzinfo, so restore UTC)."""
    return EpisodeOut(
        **ep.model_dump(exclude={"audio_path", "published_at", "listened_at", "started_at",
                                 "last_played_at"}),
        published_at=ep.published_at.replace(tzinfo=UTC),
        topic_label=TOPIC_LABELS.get(ep.topic, ep.topic),
        topic_labels=TOPIC_TRANSLATIONS.get(ep.topic, {}),
        audio_url=get_storage().url(ep.audio_path) if ep.audio_path else None,
    )


@router.get("/episodes", response_model=list[EpisodeOut])
def list_episodes(
    session: SessionDep, user: UserDep, listened: bool | None = None, topic: str | None = None
) -> list[EpisodeOut]:
    """List the current user's episodes newest first; filter with `?listened=false`
    and/or `?topic=slug`."""
    query = (
        select(Episode)
        .where(Episode.user_id == user.id, col(Episode.published_at) <= utcnow())
        .order_by(col(Episode.published_at).desc(), col(Episode.id).desc())
    )
    if listened is not None:
        query = query.where(Episode.listened == listened)
    if topic is not None:
        query = query.where(Episode.topic == topic)
    return [to_out(ep) for ep in session.exec(query)]


def own_episode(session: Session, user: User, episode_id: int) -> Episode:
    episode = session.get(Episode, episode_id)
    if episode is None or episode.user_id != user.id:  # other users' episodes look missing
        raise HTTPException(status_code=404, detail="Episode not found.")
    return episode


@router.post("/episodes/{episode_id}/progress", response_model=EpisodeOut)
def save_progress(episode_id: int, body: ProgressIn, session: SessionDep,
                  user: UserDep) -> EpisodeOut:
    """Save how far the player got: the furthest second heard (it never goes back), plus
    when it was first and last played. Feeds the start and completion rates."""
    episode = own_episode(session, user, episode_id)
    now = utcnow()
    heard = min(body.seconds, episode.duration_seconds or body.seconds)
    episode.progress_seconds = max(episode.progress_seconds, heard)
    episode.started_at = episode.started_at or now
    episode.last_played_at = now
    session.add(episode)
    session.commit()
    session.refresh(episode)
    return to_out(episode)


@router.post("/episodes/{episode_id}/listened", response_model=EpisodeOut)
def mark_listened(episode_id: int, session: SessionDep, user: UserDep) -> EpisodeOut:
    """Mark one of the current user's episodes as listened."""
    episode = own_episode(session, user, episode_id)
    episode.listened, episode.listened_at = True, utcnow()
    session.add(episode)
    session.commit()
    session.refresh(episode)
    return to_out(episode)


@router.delete("/account")
def delete_my_account(session: SessionDep, user: UserDep) -> dict[str, Any]:
    """Delete the current user and everything that is theirs (GDPR erasure). With Clerk the
    frontend then deletes the login account itself; the Clerk webhook also erases on its own
    user.deleted event, so either side can start it."""
    return {"deleted": True, "removed": delete_account(session, user)}
