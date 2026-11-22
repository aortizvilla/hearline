"""Step 2: validation, window filter, URL canonicalization, dedupe, JSON repair, costs."""

import json
from datetime import timedelta

import pytest
from sqlmodel import Session, select

from app.database.models import CostEvent, Story, utcnow
from app.pipeline.step2_research import canonicalize_url, research_topic, strip_citations
from app.providers import Providers
from app.providers.mock_llm import MockLLM
from tests.helpers import seeded, topic

HEADLINES = ["", "SpaceX tests a reusable upper stage", "Europe picks Ariane successor",
             "Mars sample return faces budget cut", "Telescope finds water vapor on exoplanet",
             "Crew returns from orbital lab", "Moon base contract awarded",
             "Asteroid flyby will be visible tonight"]


def story(n: int, **kw) -> dict:  # type: ignore[no-untyped-def,type-arg]
    base = {"headline": HEADLINES[n],
            "summary": f"Space agency reported milestone {n} today, according to Reuters.",
            "why_it_matters": "Matters.", "source_name": "Reuters",
            "url": f"https://www.reuters.com/space/{n}?utm_source=x",
            "published_date": utcnow().isoformat(), "importance": 6}
    return {**base, **kw}


@pytest.fixture
def scripted(llm: MockLLM, monkeypatch):  # type: ignore[no-untyped-def]
    replies: list[str] = []
    monkeypatch.setattr(llm, "_research", lambda req: replies.pop(0))
    monkeypatch.setattr(llm, "_research_fix", lambda req: replies.pop(0))
    return replies


def test_canonicalize_url() -> None:
    assert canonicalize_url("HTTP://WWW.Example.com/a/b/?utm_source=x&id=2&fbclid=9#frag") == \
        "https://example.com/a/b?id=2"
    assert canonicalize_url("https://m.example.com/x/amp") == "https://example.com/x"


def test_strip_citations() -> None:
    src = ("The FBI is investigating a hack. ([apnews.com](https://apnews.com/a?utm_source=x)) "
           "More from [Reuters](https://reuters.com/x), see https://example.com/y.")
    assert strip_citations(src) == "The FBI is investigating a hack. More from Reuters, see."
    assert strip_citations("Two sources ([a.com](https://a.com), [b.com](https://b.com)).") == (
        "Two sources.")


def test_skips_topics_without_subscribers(session: Session, providers: Providers,
                                          llm: MockLLM) -> None:
    seeded(session, providers)
    assert research_topic(session, topic(session, "space"), providers).skipped
    assert llm.count("research") == 0


def test_validation_window_and_url_dedupe(session: Session, providers: Providers,
                                          scripted: list[str]) -> None:
    seeded(session, providers)
    space = topic(session, "space")
    old = (utcnow() - timedelta(days=5)).isoformat()
    scripted.append(json.dumps({"stories": [
        story(1), story(2),
        story(3, url=""),                       # missing url -> dropped
        story(4, source_name=""),               # missing source -> dropped
        story(5, published_date=old),           # outside lookback -> dropped
        story(6, url="https://reuters.com/space/1"),  # same canonical url as story 1
    ]}))
    res = research_topic(session, space, providers, force=True)
    assert len(res.new_story_ids) == 2
    assert (res.invalid, res.out_of_window, res.duplicates) == (2, 1, 1)
    assert session.get(Story, res.new_story_ids[0]).canonical_url == "https://reuters.com/space/1"

    scripted.append(json.dumps({"stories": [story(1), story(7)]}))  # next run: 1 is known
    again = research_topic(session, space, providers, force=True)
    assert len(again.new_story_ids) == 1 and again.duplicates == 1


def test_headline_embedding_dedupe_and_updates(session: Session, providers: Providers,
                                               scripted: list[str]) -> None:
    seeded(session, providers)
    space = topic(session, "space")
    now = utcnow()
    scripted.append(json.dumps({"stories": [
        story(1, headline="NASA delays Artemis moon launch to next spring",
              published_date=(now - timedelta(hours=10)).isoformat()),
    ]}))
    research_topic(session, space, providers, force=True)
    scripted.append(json.dumps({"stories": [
        # same event, different outlet/URL, same time -> duplicate
        story(2, headline="NASA delays Artemis moon launch to next spring!",
              url="https://apnews.com/x", published_date=(now - timedelta(hours=9)).isoformat()),
    ]}))
    dup = research_topic(session, space, providers, force=True)
    assert dup.new_story_ids == [] and dup.duplicates == 1
    scripted.append(json.dumps({"stories": [
        # same event reported much later -> kept as an update of the original
        story(3, headline="NASA delays Artemis moon launch to next spring",
              url="https://bbc.co.uk/y", published_date=now.isoformat()),
    ]}))
    upd = research_topic(session, space, providers, force=True)
    assert upd.updates == 1
    new = session.get(Story, upd.new_story_ids[0])
    assert new.status == "update" and new.update_of_id is not None


def test_invalid_json_gets_one_corrective_retry(session: Session, providers: Providers,
                                                llm: MockLLM, scripted: list[str]) -> None:
    seeded(session, providers)
    scripted += ["Here you go: not json", json.dumps({"stories": [story(1)]})]
    res = research_topic(session, topic(session, "space"), providers, force=True)
    assert len(res.new_story_ids) == 1
    assert llm.count("research_fix") == 1
    events = session.exec(select(CostEvent).where(CostEvent.stage == "research")).all()
    assert len(events) == 2 and events[0].searches == llm.searches_per_research


def test_invalid_json_twice_raises(session: Session, providers: Providers,
                                   scripted: list[str]) -> None:
    seeded(session, providers)
    scripted += ["nope", "still nope"]
    with pytest.raises(ValueError):
        research_topic(session, topic(session, "space"), providers, force=True)
