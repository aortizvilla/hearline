"""Small builders shared by the pipeline tests."""

from datetime import datetime, timedelta

from sqlmodel import Session, select

from app.database.models import Profile, Story, Topic, User, UserTopic, utcnow
from app.pipeline.step1_topics import seed_topics
from app.providers import Providers


def make_user(session: Session, name: str = "Ana Test", tone: str = "conversational",
              minutes: int = 5, language: str = "en", voice: str = "aria") -> User:
    user = User(name=name)
    session.add(user)
    session.commit()
    session.add(Profile(user_id=user.id, tone=tone, language=language, voice=voice,
                        target_minutes=minutes))
    session.commit()
    session.refresh(user)
    return user


def topic(session: Session, key: str) -> Topic:
    return session.exec(select(Topic).where(Topic.key == key)).one()


def seeded(session: Session, providers: Providers) -> None:
    seed_topics(session, providers.embedder)


def follow(session: Session, user: User, t: Topic, keywords: list[str] | None = None,
           weight: float = 1.0) -> None:
    session.add(UserTopic(user_id=user.id, topic_id=t.id, weight=weight,
                          keyword_filters=keywords or []))
    t.active_subscribers += 1
    session.add(t)
    session.commit()


# Distinct wording per story number, so sample stories don't repeat each other's sentences
# (the accuracy review would rightly flag that as redundant)
ACTORS = ["Regulators", "A startup", "Researchers", "The ministry", "A court", "Engineers",
          "An agency", "Investors", "A university", "Lawmakers", "Doctors", "Farmers"]
THINGS = ["a licensing rule", "a funding round", "a field trial", "a safety review",
          "a pricing change", "a hiring plan", "a data audit", "a export permit",
          "a public tender", "a recall notice", "a patent filing", "a merger bid"]
PLACES = ["Berlin", "Lagos", "Osaka", "Lima", "Oslo", "Pune", "Quito", "Cairo", "Perth",
          "Seville", "Tallinn", "Hanoi"]


def add_story(session: Session, t: Topic, n: int, importance: int = 5,
              hours_ago: float = 2, headline: str | None = None,
              now: datetime | None = None) -> Story:
    now = now or utcnow()
    story = Story(
        topic_id=t.id, canonical_url=f"https://news.test/{t.key}/{n}",
        headline=headline or f"{ACTORS[n % 12]} in {PLACES[n % 12]} unveil "
                             f"{THINGS[n % 12].split(' ', 1)[1]} number {n}",
        summary=f"{ACTORS[(n + 5) % 12]} in {PLACES[(n + 7) % 12]} confirmed "
                f"{THINGS[(n + 3) % 12]} worth {n} million, according to Reuters.",
        why_it_matters="It affects many listeners.", source_name="Reuters",
        published_at=now - timedelta(hours=hours_ago), importance_score=importance,
        first_seen=now,
    )
    session.add(story)
    session.commit()
    return story
