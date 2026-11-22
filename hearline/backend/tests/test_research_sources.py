"""Research: plural sources, link checks, Wikipedia background, local-language research."""

import json
from datetime import date

import pytest
from sqlmodel import Session

from app.database.models import Story
from app.pipeline.step2_research import build_request, research_topic
from app.pipeline.step4_assemble import load_candidates
from app.providers import Providers
from app.providers.mock_llm import MockLLM
from tests.helpers import add_story, follow, make_user, seeded, topic
from tests.test_research import scripted, story  # noqa: F401  (fixture + story factory)


def test_one_outlet_cannot_dominate_and_other_sources_are_kept(
        session: Session, providers: Providers, scripted: list[str]) -> None:  # noqa: F811
    seeded(session, providers)
    space = topic(session, "space")
    scripted.append(json.dumps({"stories": [
        story(1, importance=9, other_sources=[
            {"source_name": "BBC News", "url": "https://www.bbc.com/news/space-1"},
            {"source_name": "Reuters", "url": "https://www.reuters.com/space/1"},  # = primary
            {"source_name": "", "url": "https://x.example/1"}]),  # unnamed: dropped
        story(2, importance=8), story(3, importance=2),  # 3rd Reuters story: over the cap
        story(4, source_name="AFP", url="https://www.afp.com/space/4"),
    ]}))
    res = research_topic(session, space, providers, force=True)
    assert len(res.new_story_ids) == 3 and res.over_source_cap == 1
    top = session.get(Story, res.new_story_ids[0])
    assert top.sources == [{"name": "BBC News", "url": "https://www.bbc.com/news/space-1"}]


def test_dead_links_are_dropped_and_background_is_fetched(
        session: Session, providers: Providers, scripted: list[str], monkeypatch) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    seeded(session, providers)
    space = topic(session, "space")
    monkeypatch.setattr("app.providers.web.article_link_ok", lambda url: "/space/2" not in url)
    monkeypatch.setattr("app.providers.web.wikipedia_background", lambda term, language="en": {
        "term": term, "title": "Ariane 6", "summary": "Ariane 6 is a European rocket.",
        "url": "https://en.wikipedia.org/wiki/Ariane_6", "language": language})
    scripted.append(json.dumps({"stories": [
        story(1, background_terms=["Ariane 6"]), story(2)]}))  # story 2's link is dead
    res = research_topic(session, space, providers, force=True)
    assert len(res.new_story_ids) == 1 and res.dead_links == 1
    kept = session.get(Story, res.new_story_ids[0])
    assert kept.background[0]["title"] == "Ariane 6"

    from app.pipeline.step3_scripts import story_record
    assert story_record(kept)["background"] == [
        {"term": "Ariane 6", "context": "Ariane 6 is a European rocket."}]


def test_local_language_research_uses_local_outlets_and_skips_covered_events(
        session: Session, providers: Providers) -> None:
    seeded(session, providers)
    space = topic(session, "space")
    english = add_story(session, space, 1, headline="SpaceX tests a reusable upper stage")
    now = english.first_seen
    text = build_request(session, space, providers, 24, now, "es").messages[0]["content"]
    assert "Research language: Español" in text  # the language as listeners name it
    assert "El País" in text and "SpaceX tests a reusable upper stage" in text
    english_req = build_request(session, space, providers, 24, now).messages[0]["content"]
    assert "Research language" not in english_req and "Reuters" in english_req


def test_listeners_get_english_and_their_own_language(session: Session,
                                                       providers: Providers) -> None:
    seeded(session, providers)
    space = topic(session, "space")
    en = add_story(session, space, 1)
    es = add_story(session, space, 2)
    fr = add_story(session, space, 3)
    es.language, fr.language = "es", "fr"
    session.add_all([es, fr])
    session.commit()
    maria = make_user(session, "Maria", language="es")
    follow(session, maria, space)
    cands = {c.story.id: c.score for c in load_candidates(session, maria, en.first_seen,
                                                          day=date.today())}
    assert set(cands) == {en.id, es.id}  # French stories never reach a Spanish listener
    assert cands[es.id] == pytest.approx(1.5 * cands[en.id], rel=1e-4)  # local news ranks higher


def test_a_local_story_is_written_first_in_its_own_language(
        session: Session, providers: Providers, llm: MockLLM) -> None:
    from app.pipeline.step3_scripts import ensure_clips

    seeded(session, providers)
    s = add_story(session, topic(session, "space"), 1)
    s.language = "es"
    session.add(s)
    session.commit()
    specs = [(s, "conversational", "en", "aria"), (s, "conversational", "es", "aria")]
    ensure_clips(session, specs, providers, synthesize=False)
    written = [c for c in llm.calls if c.purpose == "clip_write"]
    assert len(written) == 1 and written[0].context["language"] == "es"  # base in Spanish
    assert llm.count("clip_adapt") == 1  # English adapted from it


def test_research_units_add_listener_languages(session: Session, providers: Providers) -> None:
    from app.database.models import utcnow
    from app.jobs.scheduler import research_key, research_units

    seeded(session, providers)
    space = topic(session, "space")
    for name, lang in [("Ana", "es"), ("Bob", "en"), ("Cleo", "es")]:
        follow(session, make_user(session, name, language=lang), space)
    assert research_units(session, ["space"], utcnow()) == [("space", "en"), ("space", "es")]
    assert research_key("space", "en", "2026-11-22") == "research:space:2026-11-22"
    assert research_key("space", "es", "2026-11-22") == "research:space:es:2026-11-22"


def test_off_topic_stories_and_known_events_are_dropped(
        session: Session, providers: Providers, scripted: list[str]) -> None:  # noqa: F811
    seeded(session, providers)
    space = topic(session, "space")
    scripted.append(json.dumps({"stories": [story(1)]}))
    assert len(research_topic(session, space, providers, force=True).new_story_ids) == 1
    scripted.append(json.dumps({"stories": [
        story(2, headline="Off-topic: diplomats meet about trade"),  # relevance check drops it
        story(3, other_sources=[{"source_name": "Reuters",  # same event as story 1
                                 "url": "https://www.reuters.com/space/1"}]),
        story(4)]}))
    res = research_topic(session, space, providers, force=True)
    assert (res.off_topic, res.duplicates, len(res.new_story_ids)) == (1, 1, 1)


def test_article_links_are_told_apart_from_section_pages() -> None:
    from app.providers.web import looks_like_article

    assert not looks_like_article("https://elpais.com/noticias/inteligencia-artificial")
    assert not looks_like_article("https://lanacion.com.ar/tecnologia")
    assert looks_like_article("https://techcrunch.com/2026/11/21/a-tale-of-two-nvidias")
    assert looks_like_article("https://apnews.com/article/d066cff87a64a564db0a00172c124972")


def test_one_event_is_heard_once_even_under_two_topics(session: Session,
                                                       providers: Providers) -> None:
    seeded(session, providers)
    tech, business = topic(session, "technology"), topic(session, "business")
    same = providers.embedder.embed_one("nvidia stops selling h200 chips in china")
    a = add_story(session, tech, 1, importance=8, headline="Nvidia stops selling H200 in China")
    b = add_story(session, business, 2, importance=6, headline="Nvidia halts H200 China sales")
    c = add_story(session, business, 3, importance=5)
    a.embedding = b.embedding = same  # two outlets, two topics, one event
    c.embedding = providers.embedder.embed_one("retail sales rose in october")
    session.add_all([a, b, c])
    session.commit()
    ana = make_user(session, "Ana")
    follow(session, ana, tech)
    follow(session, ana, business)
    ids = {cand.story.id for cand in load_candidates(session, ana, a.first_seen,
                                                     day=date.today())}
    assert ids == {a.id, c.id}  # the better-ranked telling of the event is kept


def test_trusted_outlets_rank_higher(session: Session, providers: Providers,
                                     monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.config import pipeline_cfg
    from app.pipeline.step4_assemble import source_weight

    monkeypatch.setattr(pipeline_cfg().research, "source_weights",
                        {"apnews.com": 1.2, "tabloid.example": 0.6})
    assert source_weight("https://apnews.com/article/abc123") == 1.2
    assert source_weight("https://www.tabloid.example/story-x") == 0.6
    assert source_weight("https://news.tabloid.example/a") == 0.6  # subdomains too
    assert source_weight("https://elpais.com/x") == 1.0  # unlisted: neutral
