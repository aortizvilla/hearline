"""Accuracy review of the whole episode before recording (app/pipeline/review.py)."""

import pytest
from sqlmodel import Session, select

from app.database.models import Episode, EpisodeReview, EpisodeStory, PhraseClip
from app.pipeline.review import repeated_sentences
from app.pipeline.step4_assemble import assemble_episode
from app.providers import Providers
from app.providers.llm import LLMError, TransientLLMError
from app.providers.mock_llm import MockLLM
from tests.helpers import add_story, follow, make_user, seeded, topic


def _user_with_stories(session: Session, providers: Providers, flagged: str = "") -> tuple:  # type: ignore[type-arg]
    seeded(session, providers)
    ai = topic(session, "ai")
    user = make_user(session, "Ana", minutes=5)
    follow(session, user, ai)
    stories = [add_story(session, ai, n, importance=6 + n) for n in (1, 2, 3)]
    if flagged:
        stories[2].summary += f" {flagged}"  # the best story: its script carries the word
        session.add(stories[2])
        session.commit()
    return user, stories


def test_flagged_story_is_removed_and_the_episode_replanned(session: Session,
                                                           providers: Providers) -> None:
    user, stories = _user_with_stories(session, providers, flagged="FAKE claim.")
    ep = assemble_episode(session, user.id, providers)
    assert isinstance(ep, Episode) and ep.status == "ready"
    read = set(session.exec(select(EpisodeStory.story_id).where(
        EpisodeStory.episode_id == ep.id)))
    assert stories[2].id not in read and read  # out, and the others still make an episode
    assert ep.quality["removed"] == [stories[2].id]
    assert any(i["type"] == "unsupported" for i in ep.quality["issues"])
    assert not ep.quality["clean_first_time"]


def test_clean_episode_is_marked_clean_and_its_review_cached(session: Session,
                                                            providers: Providers,
                                                            llm: MockLLM) -> None:
    user, _ = _user_with_stories(session, providers)
    plan = assemble_episode(session, user.id, providers, dry_run=True)
    assert plan.frame["quality"]["clean_first_time"]  # type: ignore[union-attr]
    calls = llm.count("episode_review")
    assemble_episode(session, user.id, providers, dry_run=True)  # same content again
    assert llm.count("episode_review") == calls  # served from dim_episode_review
    assert len(session.exec(select(EpisodeReview)).all()) == 1


def test_flagged_teaser_is_dropped_and_forgotten(session: Session, providers: Providers) -> None:
    user, stories = _user_with_stories(session, providers)
    stories[1].headline += " FAKE"  # only its teaser (a headline read aloud) says it
    session.add(stories[1])
    session.commit()
    plan = assemble_episode(session, user.id, providers, dry_run=True)
    quality = plan.frame["quality"]  # type: ignore[union-attr]
    assert stories[1].id in quality["teasers_dropped"]
    assert stories[1].id not in plan.frame["teaser_ids"]  # type: ignore[union-attr]
    assert not session.exec(select(PhraseClip).where(
        PhraseClip.key == f"teaser:story:{stories[1].id}")).all()


def test_repeated_sentence_is_caught_for_free() -> None:
    same = "The regional parliament approved the new housing budget for northern districts."
    segs = [{"id": "story:1", "kind": "story", "text": same + " Other words follow."},
            {"id": "story:2", "kind": "story", "text": "Meanwhile, " + same.lower()},
            {"id": "teaser:1", "kind": "teaser", "text": same}]
    issues = repeated_sentences(segs)
    assert [(i["segment"], i["type"]) for i in issues] == [("story:2", "redundant")]


def test_a_failed_review_never_records_an_episode(session: Session, providers: Providers,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    user, _ = _user_with_stories(session, providers)

    def broken(self, req):  # type: ignore[no-untyped-def]
        raise LLMError("review model down")
    monkeypatch.setattr(MockLLM, "_episode_review", broken)
    with pytest.raises(TransientLLMError):
        assemble_episode(session, user.id, providers)
    assert not session.exec(select(Episode)).all()
