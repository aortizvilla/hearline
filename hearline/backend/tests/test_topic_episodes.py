"""Per-topic episodes: next to the daily briefing, one episode per followed topic."""

from sqlmodel import Session, select

from app.database.models import Episode, EpisodeStory, Job, UserTopic
from app.jobs.scheduler import plan_daily, register_handlers
from app.jobs.worker import run_worker
from app.pipeline.step4_assemble import assemble_episode
from app.providers import Providers
from tests.helpers import add_story, follow, make_user, seeded, topic


def _follow(session: Session, user, key: str, interest: str) -> None:  # type: ignore[no-untyped-def]
    follow(session, user, topic(session, key))
    ut = session.exec(select(UserTopic).where(UserTopic.user_id == user.id,
                                              UserTopic.topic_id == topic(session, key).id)).one()
    ut.interest = interest
    session.add(ut)
    session.commit()


def test_topic_episode_has_only_its_topic_and_its_own_ledger(session: Session,
                                                              providers: Providers) -> None:
    seeded(session, providers)
    ana = make_user(session, "Ana", minutes=5)
    _follow(session, ana, "ai", "ai")
    _follow(session, ana, "climate", "environment")
    for n in range(3):
        add_story(session, topic(session, "ai"), n, importance=6)
        add_story(session, topic(session, "climate"), 10 + n, importance=6)

    briefing = assemble_episode(session, ana.id, providers)
    assert isinstance(briefing, Episode) and briefing.topic == "briefing"
    ai_ep = assemble_episode(session, ana.id, providers, topic="ai")
    assert isinstance(ai_ep, Episode) and ai_ep.topic == "ai" and ai_ep.status == "ready"
    assert ai_ep.title.startswith("Your ") and "update" in ai_ep.title
    ids = session.exec(select(EpisodeStory.story_id).where(
        EpisodeStory.episode_id == ai_ep.id)).all()
    ai_id = topic(session, "ai").id
    from app.database.models import Story
    assert ids and all(session.get(Story, i).topic_id == ai_id for i in ids)  # type: ignore[union-attr]
    # the briefing having read a story doesn't hide it from the topic episode
    briefing_ids = set(session.exec(select(EpisodeStory.story_id).where(
        EpisodeStory.episode_id == briefing.id)))
    assert set(ids) & briefing_ids
    # but a second topic episode doesn't repeat it
    assert assemble_episode(session, ana.id, providers, topic="ai") is None


def test_daily_plan_builds_briefing_then_one_episode_per_topic(engine, session: Session,  # type: ignore[no-untyped-def]
                                                               providers: Providers) -> None:
    register_handlers()
    seeded(session, providers)
    ana, ben = make_user(session, "Ana"), make_user(session, "Ben")
    _follow(session, ana, "ai", "ai")
    _follow(session, ana, "climate", "environment")
    _follow(session, ben, "ai", "ai")  # one topic: the briefing is already that episode
    for n in range(3):
        add_story(session, topic(session, "ai"), n, importance=6)
        add_story(session, topic(session, "climate"), 10 + n, importance=6)
    plan_daily(session, run_immediately=True)
    stats = run_worker(engine, providers, stop_when_empty=True)
    assert stats.failed == 0
    kinds = {(e.user_id, e.topic) for e in session.exec(
        select(Episode).where(Episode.status == "ready"))}
    assert kinds == {(ana.id, "briefing"), (ana.id, "ai"), (ana.id, "environment"),
                     (ben.id, "briefing")}
    # running again queues nothing new
    before = len(session.exec(select(Job)).all())
    run_worker(engine, providers, stop_when_empty=True)
    assert len(session.exec(select(Job)).all()) == before
