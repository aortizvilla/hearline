"""Slow news days: earlier unheard news fills first, then Wikipedia explainers, else shorter."""

import json
from datetime import date

from sqlmodel import Session

from app.config import pipeline_cfg
from app.database.models import Story
from app.pipeline.step2_research import research_topic
from app.pipeline.step4_assemble import load_candidates, select_stories
from app.providers import Providers
from tests.helpers import add_story, follow, make_user, seeded, topic
from tests.test_research import scripted, story  # noqa: F401  (fixture + story factory)


def explainer(session: Session, t, n: int, of: Story | None = None) -> Story:  # type: ignore[no-untyped-def]
    """A background explainer; `of`: the news story whose background it is."""
    s = add_story(session, t, 100 + n, importance=1, headline=f"Background term {n}")
    s.status, s.source_name = "explainer", "Wikipedia"
    session.add(s)
    if of is not None:
        of.background = [*(of.background or []), {"term": f"term {n}", "url": s.canonical_url,
                                                  "summary": "x"}]
        session.add(of)
    session.commit()
    return s


def test_research_turns_wikipedia_background_into_explainers(
        session: Session, providers: Providers, scripted: list[str], monkeypatch) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    seeded(session, providers)
    space = topic(session, "space")
    monkeypatch.setattr("app.providers.web.wikipedia_background", lambda term, language="en": {
        "term": term, "title": "Ariane 6", "summary": "Ariane 6 is a European rocket.",
        "url": "https://en.wikipedia.org/wiki/Ariane_6", "language": language})
    scripted.append(json.dumps({"stories": [story(1, background_terms=["Ariane 6"]),
                                            story(2, background_terms=["Ariane 6"])]}))
    research_topic(session, space, providers, force=True)
    from sqlmodel import select
    found = session.exec(select(Story).where(Story.status == "explainer")).all()
    assert len(found) == 1  # one per Wikipedia article, however many stories mention it
    assert (found[0].source_name, found[0].headline) == ("Wikipedia", "Ariane 6")


def test_a_slow_day_fills_with_earlier_news_then_explainers(session: Session,
                                                             providers: Providers) -> None:
    pipeline_cfg().assemble.max_explainers = 2  # off by default; the feature stays
    seeded(session, providers)
    ai = topic(session, "ai")
    today = add_story(session, ai, 1, importance=6)
    older = add_story(session, ai, 2, importance=8, hours_ago=70)  # 3 days old, unheard
    gone = add_story(session, ai, 3, importance=9, hours_ago=120)  # beyond the backfill
    e1, e2, e3 = (explainer(session, ai, n, of=today if n < 3 else older) for n in (1, 2, 3))
    orphan = explainer(session, ai, 4)  # nobody's background: never read on its own
    user = make_user(session, "Ana", minutes=10)
    follow(session, user, ai)
    cands = load_candidates(session, user, today.first_seen, day=date.today())
    assert gone.id not in {c.story.id for c in cands}
    chosen = [c.story.id for c in select_stories(cands, 10 * 60)]
    news = [i for i in chosen if i in (today.id, older.id)]
    assert news == [today.id, older.id]  # today's news first, then earlier news
    assert len([i for i in chosen if i in (e1.id, e2.id, e3.id)]) == 2  # at most 2 explainers
    assert orphan.id not in chosen
    for e in (e1.id, e2.id):  # each right after the story it explains
        if e in chosen:
            assert chosen[chosen.index(e) - 1] in (today.id, e1.id)


def test_explainers_never_displace_news(session: Session, providers: Providers) -> None:
    seeded(session, providers)
    ai = topic(session, "ai")
    news = [add_story(session, ai, n, importance=6) for n in range(1, 6)]
    explainer(session, ai, 1)
    user = make_user(session, "Ana", minutes=3)
    follow(session, user, ai)
    cands = load_candidates(session, user, news[0].first_seen, day=date.today())
    chosen = select_stories(cands, 3 * 60)
    assert all(c.story.status != "explainer" for c in chosen)  # enough news: no explainer


def test_the_rundown_never_teases_an_explainer(session: Session, providers: Providers) -> None:
    from app.pipeline.step4_assemble import assemble_episode

    pipeline_cfg().assemble.max_explainers = 2  # off by default; the feature stays
    seeded(session, providers)
    ai = topic(session, "ai")
    first = [add_story(session, ai, n, importance=6) for n in (1, 2)][0]
    explainer(session, ai, 1, of=first)
    user = make_user(session, "Ana", minutes=10)
    follow(session, user, ai)
    plan = assemble_episode(session, user.id, providers, dry_run=True)
    assert any(c.story.status == "explainer" for c, _ in plan.items)  # it fills the episode
    assert not any("Background term" in line for line in plan.frame["rundown"])

