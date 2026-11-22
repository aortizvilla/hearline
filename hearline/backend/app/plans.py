"""Subscription plans: what each plan allows (config/plans.yaml) and keeping users within it.

Used by the API (validate Settings, change plan) and step 4 (cap the episode).
There is no billing yet: plans change through set_plan (admin endpoint, CLI, or the
Subscription page when self-serve is on).
"""

from __future__ import annotations

from sqlmodel import Session, col, select

from app.config import PlanCfg, plans_cfg
from app.database.models import Subscription, User, UserTopic, utcnow
from app.pipeline.step1_topics import refresh_active_subscribers


def plan_of(user: User) -> PlanCfg:
    return plans_cfg().get(user.plan)


def plan_error(plan: PlanCfg, tone: str, voice: str,
               topics: list[tuple[int, list[str]]]) -> str | None:
    """Why these Settings don't fit the plan (None if they do).
    topics: (minutes, subcategories) per followed topic."""
    name = plan.label
    if plan.max_topics is not None and len(topics) > plan.max_topics:
        return f"The {name} plan includes up to {plan.max_topics} topics."
    if any(minutes > plan.max_topic_minutes for minutes, _ in topics):
        return f"The {name} plan allows up to {plan.max_topic_minutes} minutes per topic."
    if not plan.subtopics and any(subs for _, subs in topics):
        return f"Subcategories are not included in the {name} plan."
    if not plan.allows_voice(voice):
        return f"This voice is not included in the {name} plan."
    if not plan.allows_tone(tone):
        return f"This tone is not included in the {name} plan."
    return None


def fit_to_plan(session: Session, user: User) -> None:
    """Bring a user's Settings within their plan (after a downgrade, or a new user's default
    profile): keep the first topics, shorten long ones, drop subcategories, and switch to the
    plan's first voice / tone if theirs isn't included. Never adds anything."""
    plan = plan_of(user)
    subs = session.exec(select(UserTopic).where(UserTopic.user_id == user.id)
                        .order_by(col(UserTopic.id))).all()
    for i, ut in enumerate(subs):
        if plan.max_topics is not None and i >= plan.max_topics:
            session.delete(ut)
            continue
        if ut.target_minutes and ut.target_minutes > plan.max_topic_minutes:
            ut.target_minutes = plan.max_topic_minutes
            ut.weight = ut.target_minutes / 10  # same rule as set_interests
        if not plan.subtopics:
            ut.subtopics = []
        session.add(ut)
    profile = user.profile
    if profile is not None:
        # not allowed means the plan has a list: switch to its first entry
        if not plan.allows_voice(profile.voice) and plan.voices != "all":
            profile.voice = plan.voices[0]
        if not plan.allows_tone(profile.tone) and plan.tones != "all":
            profile.tone = plan.tones[0]
        profile.target_minutes = min(profile.target_minutes, plan.max_topic_minutes)
        session.add(profile)
    session.commit()
    refresh_active_subscribers(session)
    session.refresh(user)


def active_subscription(session: Session, user: User) -> Subscription | None:
    return session.exec(select(Subscription).where(
        Subscription.user_id == user.id, Subscription.status == "active")).first()


def start_subscription(session: Session, user: User, plan_key: str, source: str) -> Subscription:
    """End the user's active subscription (if any) and open one on `plan_key`; User.plan
    follows. Commits."""
    now = utcnow()
    current = active_subscription(session, user)
    if current is not None:
        current.status, current.ended_at = "ended", now
        session.add(current)
    sub = Subscription(user_id=user.id, plan=plan_key, started_at=now, source=source,  # type: ignore[arg-type]
                       price_usd_month=plans_cfg().plans[plan_key].price_usd_month)
    user.plan = plan_key
    session.add_all([sub, user])
    session.commit()
    return sub


def set_plan(session: Session, user: User, plan_key: str, source: str) -> User:
    """Move a user to another plan and fit their Settings to it (ValueError if unknown).
    Choosing the plan they're already on changes nothing."""
    if plan_key not in plans_cfg().plans:
        raise ValueError(f"Unknown plan {plan_key!r}.")
    if plan_key != user.plan or active_subscription(session, user) is None:
        start_subscription(session, user, plan_key, source)
    fit_to_plan(session, user)
    return user


def plans_for_ui() -> list[dict[str, object]]:
    """Every plan with its limits, in config order, for the Subscription and Settings pages."""
    return [plan.model_dump() for plan in plans_cfg().plans.values()]
