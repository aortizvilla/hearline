"""Subcategories: classified once and shared, tagged on stories, steer research and ranking."""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.database.models import Subtopic
from app.pipeline.step1_topics import (
    SubtopicError,
    keyword_hints,
    resolve_subtopic,
    set_interests,
    tag_stories,
)
from app.pipeline.step4_assemble import load_candidates
from app.providers import Providers
from app.providers.mock_llm import MockLLM
from tests.helpers import add_story, make_user, seeded, topic


def test_a_phrase_is_classified_once_then_reused(session: Session, providers: Providers,
                                                  llm: MockLLM) -> None:
    seeded(session, providers)
    film = topic(session, "film-tv")
    cannes = resolve_subtopic(session, film, "Cannes Festival", providers)
    assert (cannes.label, cannes.topic_id) == ("Cannes Festival", film.id)
    assert llm.count("subtopic_resolve") == 1
    again = resolve_subtopic(session, film, "  cannes festival ", providers)
    assert again.id == cannes.id and llm.count("subtopic_resolve") == 1  # known phrase: free
    with pytest.raises(SubtopicError, match="doesn't fit"):
        resolve_subtopic(session, film, "unrelated stock tips", providers)


def test_settings_save_subcategories_and_reject_ones_that_dont_fit(client: TestClient) -> None:
    profile = client.get("/api/profile").json()
    topics = [{"topic": "film", "frequency": "daily", "target_minutes": 10,
               "subtopics": ["Cannes Festival", "Oscars"]}]
    saved = client.put("/api/profile", json={**profile, "topics": topics})
    assert saved.status_code == 200
    # "Cannes Festival" joins the suggested "Cannes Film Festival" (canonical name returned)
    assert saved.json()["topics"][0]["subtopics"] == ["Cannes Film Festival", "Oscars"]

    bad = [{**topics[0], "subtopics": ["unrelated stock tips"]}]
    resp = client.put("/api/profile", json={**profile, "name": "Changed", "topics": bad})
    assert resp.status_code == 422 and "doesn't fit" in resp.json()["detail"]
    after = client.get("/api/profile").json()
    assert after["name"] != "Changed" and after["topics"][0]["subtopics"] == [
        "Cannes Film Festival", "Oscars"]  # nothing half-saved


def test_stories_are_tagged_and_ranked_higher_for_listeners_who_focus(
        session: Session, providers: Providers) -> None:
    from datetime import date

    seeded(session, providers)
    film = topic(session, "film-tv")
    plain = add_story(session, film, 1, headline="Box office weekend results are in")
    cannes_story = add_story(session, film, 2, headline="Cannes Festival unveils its jury")
    fan, other = make_user(session, "Fan"), make_user(session, "Other")
    set_interests(session, fan.id, [("film", "daily", 10, ["Cannes Festival"])], providers)  # type: ignore[arg-type]
    set_interests(session, other.id, [("film", "daily", 10)], providers)  # type: ignore[arg-type]
    session.refresh(cannes_story)
    assert cannes_story.subtopics == ["cannes-festival"] and not plain.subtopics  # tagged

    def score(user, story):  # type: ignore[no-untyped-def]
        cands = load_candidates(session, user, plain.first_seen, day=date.today())
        return next(c.score for c in cands if c.story.id == story.id)
    assert score(fan, cannes_story) == 2 * score(other, cannes_story)  # focus doubles it
    assert score(fan, plain) == score(other, plain)  # the rest of Film still plays

    assert "Cannes Festival" in keyword_hints(session, film.id, 10)  # research covers it  # type: ignore[arg-type]


def test_tagging_matches_whole_words_only(session: Session, providers: Providers) -> None:
    seeded(session, providers)
    film = topic(session, "film-tv")
    sub = Subtopic(topic_id=film.id, key="oscars", label="Oscars", keywords=["oscars"])  # type: ignore[arg-type]
    story = add_story(session, film, 1, headline="Oscarsson wins chess title")
    tag_stories([story], [sub])
    assert story.subtopics == []
    assert session.exec(select(Subtopic)).all() == []  # tagging never writes subtopics


def test_every_settings_topic_suggests_its_own_subcategories(client: TestClient,
                                                             llm: MockLLM) -> None:
    interests = {i["slug"]: i for i in client.get("/api/interests").json()}
    assert "history" in interests and "law" in interests  # the new topics
    assert all(len(i["suggestions"]) >= 3 for i in interests.values())
    labels = {slug: [s["label"] for s in i["suggestions"]] for slug, i in interests.items()}
    assert "World War II" in labels["history"]
    assert labels["film"][0] == "Cannes Film Festival"
    suggestion_sets = [tuple(v) for v in labels.values()]
    assert len(set(suggestion_sets)) == len(suggestion_sets)  # different for each topic

    profile = client.get("/api/profile").json()
    topics = [{"topic": "history", "frequency": "weekly", "target_minutes": 10,
               "subtopics": ["World War II", "Ancient Rome"]}]
    saved = client.put("/api/profile", json={**profile, "topics": topics}).json()
    assert saved["topics"][0]["subtopics"] == ["World War II", "Ancient Rome"]
    assert llm.count("subtopic_resolve") == 0  # picking a suggestion costs nothing


def test_seeding_suggestions_is_idempotent(session: Session, providers: Providers) -> None:
    from app.pipeline.step1_topics import seed_subtopics

    seeded(session, providers)
    first = seed_subtopics(session, providers.embedder)
    assert first > 100 and seed_subtopics(session, providers.embedder) == 0


def test_topics_and_subcategories_are_translated(client: TestClient) -> None:
    from app.config import language_names

    langs = set(language_names())
    interests = {i["slug"]: i for i in client.get("/api/interests").json()}
    assert all(set(i["labels"]) == langs for i in interests.values())  # stored on dim_topic
    assert interests["history"]["labels"]["es"] == "Historia"
    ww2 = next(s for s in interests["history"]["suggestions"] if s["label"] == "World War II")
    assert ww2["labels"]["de"] == "Zweiter Weltkrieg" and set(ww2["labels"]) == langs

    profile = client.get("/api/profile").json()
    topics = [{"topic": "history", "frequency": "weekly", "target_minutes": 10,
               "subtopics": ["World War II"]}]
    saved = client.put("/api/profile", json={**profile, "topics": topics}).json()
    assert saved["topics"][0]["subtopic_labels"]["World War II"]["fr"] == (
        "Seconde Guerre mondiale")

