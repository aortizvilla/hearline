"""Topic resolution: alias -> embedding -> one LLM call, and every decision is cached."""

from sqlmodel import Session, select

from app.database.models import Topic, TopicAlias, UserTopic
from app.pipeline.step1_topics import normalize, resolve_interest, resolve_topic, subscribe
from app.providers import Providers
from app.providers.mock_llm import MockLLM
from tests.helpers import make_user, seeded, topic


def test_normalize() -> None:
    assert normalize("  Fútbol,  LA Liga! ") == "futbol la liga"


def test_exact_alias_costs_nothing(session: Session, providers: Providers, llm: MockLLM) -> None:
    seeded(session, providers)
    assert resolve_topic(session, "Artificial Intelligence", providers).key == "ai"
    assert resolve_topic(session, "  machine-learning ", providers).key == "ai"
    assert llm.count("topic_resolve") == 0


def test_embedding_match_stores_alias(session: Session, providers: Providers,
                                      llm: MockLLM) -> None:
    seeded(session, providers)
    ai = topic(session, "ai")
    phrase = "ai newsletter stuff"
    ai.embedding = providers.embedder.embed_one(phrase)  # make it the nearest neighbour
    session.add(ai)
    session.commit()
    res = resolve_interest(session, phrase, providers)
    assert (res.topic.key, res.method) == ("ai", "embedding")
    assert llm.count("topic_resolve") == 0
    assert session.exec(select(TopicAlias).where(
        TopicAlias.alias_normalized == phrase)).one().source == "embedding"


def test_llm_fallback_is_called_once_per_phrase(session: Session, providers: Providers,
                                                llm: MockLLM) -> None:
    seeded(session, providers)
    first = resolve_interest(session, "Underwater basket weaving", providers)
    assert first.method == "llm" and llm.count("topic_resolve") == 1
    second = resolve_interest(session, "underwater  BASKET weaving", providers)
    assert second.method == "alias" and second.topic.id == first.topic.id
    assert llm.count("topic_resolve") == 1  # cached: never costs again


def test_llm_decisions_same_as_child_of_new(session: Session, providers: Providers,
                                            llm: MockLLM, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seeded(session, providers)
    answers = iter(['{"same_as": "space"}', '{"child_of": "motorsport"}',
                    '{"new": {"key": "beekeeping", "display_name": "Beekeeping"}}'])
    monkeypatch.setattr(llm, "_topic_resolve", lambda req: next(answers))

    assert resolve_interest(session, "rocket launches xyz", providers).topic.key == "space"
    niche = resolve_interest(session, "Formula One racing", providers)
    assert (niche.topic.key, niche.keyword) == ("motorsport", "formula one racing")
    new = resolve_interest(session, "bees", providers)
    assert new.topic.key == "beekeeping"
    assert session.exec(select(Topic).where(Topic.key == "beekeeping")).one()


def test_niche_becomes_keyword_filter_and_broad_clears_it(session: Session, providers: Providers,
                                                          llm: MockLLM, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seeded(session, providers)
    monkeypatch.setattr(llm, "_topic_resolve", lambda req: '{"child_of": "motorsport"}')
    user = make_user(session)
    ut = subscribe(session, user.id, "Formula One racing", providers)
    assert ut.keyword_filters == ["formula one racing"]
    subscribe(session, user.id, "motorsport", providers)  # broad subscription: all stories
    rows = session.exec(select(UserTopic).where(UserTopic.user_id == user.id)).all()
    assert len(rows) == 1 and rows[0].keyword_filters == []


def test_settings_choices_are_classified_like_any_interest(session: Session,
                                                           providers: Providers, llm: MockLLM,
                                                           monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.pipeline.step1_topics import set_interests

    seeded(session, providers)
    monkeypatch.setattr(llm, "_topic_resolve", lambda req: '{"child_of": "motorsport"}')
    user = make_user(session)
    subs = set_interests(session, user.id, [("technology", "daily", 10),
                                            ("Formula One racing", "daily", 5)], providers)
    by_key = {session.get(Topic, ut.topic_id).key: ut for ut in subs}  # type: ignore[union-attr]
    assert by_key["technology"].keyword_filters == []  # catalog slug: free alias match
    assert by_key["motorsport"].keyword_filters == ["formula one racing"]  # step 1 LLM decision
    assert topic(session, "motorsport").active_subscribers == 1  # step 2 will research it
