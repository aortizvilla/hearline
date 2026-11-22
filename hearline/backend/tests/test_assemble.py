"""Steps 4-6: candidate selection, per-user exclusion, length fitting, skipping, dry run."""

from sqlmodel import Session, select

from app.config import pipeline_cfg
from app.database.models import CostEvent, Episode, EpisodeStory, Topic, utcnow
from app.pipeline.step4_assemble import (
    Candidate,
    assemble_episode,
    episode_sources,
    episode_transcript,
    load_candidates,
    select_stories,
)
from app.providers import Providers
from app.providers.mock_llm import MockLLM
from tests.helpers import add_story, follow, make_user, seeded, topic


def cand(t: Topic, n: int, score: float, seconds: float = 40) -> Candidate:
    from app.database.models import Story, utcnow
    s = Story(id=n, topic_id=t.id, canonical_url=f"u{n}", headline=f"h{n}", summary="s",
              source_name="R", published_at=utcnow())
    return Candidate(s, t, score, seconds)


def test_length_fitting_covers_topics_and_caps_per_topic() -> None:
    a, b, c = Topic(id=1, key="a", display_name="A"), Topic(id=2, key="b", display_name="B"), \
        Topic(id=3, key="c", display_name="C")
    cands = [cand(a, i, 10 - i) for i in range(1, 9)] + [cand(b, 20, 1), cand(c, 30, 0.5)]
    cfg = pipeline_cfg().assemble
    cfg.max_per_topic, cfg.frame_overhead_seconds, cfg.transition_seconds = 3, 20, 5
    chosen = select_stories(cands, target_seconds=5 * 60)
    keys = [x.topic.key for x in chosen]
    assert set(keys) == {"a", "b", "c"}  # low-scoring topics still get one story
    assert keys.count("a") == 3  # cap per topic
    assert keys == sorted(keys, key=["a", "b", "c"].index)  # grouped by topic
    total = sum(x.est_seconds for x in chosen) + 3 * 5 + 20
    assert total <= 5 * 60

    short = select_stories(cands, target_seconds=90)  # budget for ~1 story
    assert [x.story.id for x in short] == [1]  # the best story overall
    assert select_stories([cand(a, 1, 5, seconds=500)], 60)  # never empty if candidates exist


def test_recently_heard_stories_are_excluded_per_user(session: Session,
                                                      providers: Providers) -> None:
    seeded(session, providers)
    ai = topic(session, "ai")
    ana, ben = make_user(session, "Ana"), make_user(session, "Ben")
    follow(session, ana, ai)
    follow(session, ben, ai)
    for n in range(3):
        add_story(session, ai, n, importance=5 + n)

    ep = assemble_episode(session, ana.id, providers)
    assert isinstance(ep, Episode) and ep.status == "ready"
    heard = {r.story_id for r in session.exec(select(EpisodeStory))}
    assert heard and not heard & {c.story.id for c in load_candidates(session, ana, ep.created_at)}
    # Ben has not heard them: same stories (and the same cached clips) are available to him
    assert {c.story.id for c in load_candidates(session, ben, ep.created_at)} >= heard


def test_keyword_filters_restrict_niche_subscriptions(session: Session,
                                                      providers: Providers) -> None:
    seeded(session, providers)
    sports = topic(session, "sports")
    user = make_user(session)
    follow(session, user, sports, keywords=["formula 1"])
    add_story(session, sports, 1, headline="Formula 1 title decided in Abu Dhabi")
    add_story(session, sports, 2, headline="Local marathon draws record crowd")
    assert [c.story.id for c in load_candidates(session, user, add_story(
        session, sports, 3, headline="Golf open postponed").first_seen)] == [1]


def test_user_without_unseen_stories_is_skipped(session: Session, providers: Providers,
                                                llm: MockLLM) -> None:
    seeded(session, providers)
    user = make_user(session)
    follow(session, user, topic(session, "ai"))
    assert assemble_episode(session, user.id, providers) is None  # no stories at all
    add_story(session, topic(session, "ai"), 1)
    assert assemble_episode(session, user.id, providers) is not None
    calls = len(llm.calls)
    assert assemble_episode(session, user.id, providers) is None  # everything heard
    assert len(llm.calls) == calls  # no filler episode, no LLM spend
    assert len(session.exec(select(Episode)).all()) == 1


def test_unverified_clips_never_reach_episodes(session: Session, providers: Providers,
                                               llm: MockLLM) -> None:
    seeded(session, providers)
    user = make_user(session)
    ai = topic(session, "ai")
    follow(session, user, ai)
    bad, good = add_story(session, ai, 1, importance=9), add_story(session, ai, 2)
    llm.fail_verify_story_ids = {bad.id}
    ep = assemble_episode(session, user.id, providers)
    used = [r.story_id for r in session.exec(select(EpisodeStory))]
    assert used == [good.id] and bad.headline not in episode_transcript(session, ep)


def test_dry_run_persists_nothing_and_skips_tts(session: Session, providers: Providers) -> None:
    seeded(session, providers)
    user = make_user(session)
    follow(session, user, topic(session, "ai"))
    add_story(session, topic(session, "ai"), 1)
    plan = assemble_episode(session, user.id, providers, dry_run=True)
    assert plan.transcript.startswith("[Intro]") and "Source: Reuters" in plan.transcript
    assert not session.exec(select(Episode)).all()
    assert not session.exec(select(EpisodeStory)).all()
    assert all(clip.audio_path is None for _, clip in plan.items)


def test_episode_has_transcript_sources_and_audio(session: Session,
                                                  providers: Providers) -> None:
    from app.config import get_settings
    seeded(session, providers)
    user = make_user(session, minutes=3)
    for n, key in enumerate(("ai", "space"), start=1):
        follow(session, user, topic(session, key))
        add_story(session, topic(session, key), n)
    ep = assemble_episode(session, user.id, providers)
    assert ep.duration_seconds and (get_settings().audio_dir / ep.audio_path).exists()
    assert ep.audio_path.startswith(f"users/{user.id}/episodes/{ep.id}.")
    assert (ep.tone, ep.language, ep.voice) == (user.profile.tone, user.profile.language,
                                                user.profile.voice)
    sources, script = episode_sources(session, ep), episode_transcript(session, ep)
    assert {s["topic"] for s in sources} == {"ai", "space"}
    assert all(s["url"].startswith("https://") for s in sources)
    assert "[Artificial Intelligence]" in script and "[Outro]" in script
    assert script.count("(Source: ") == len(sources)  # every fact traceable
    assert len(str(ep.frame)) < 1000  # only the frame is stored on the episode
    assert ep.image_url and ep.image_url.startswith("https://")  # first topic's picture


def test_episode_uses_the_latest_profile_even_if_a_stale_copy_is_cached(
        session: Session, providers: Providers) -> None:
    from sqlalchemy import text
    seeded(session, providers)
    user = make_user(session, voice="aria", tone="conversational")
    follow(session, user, topic(session, "ai"))
    add_story(session, topic(session, "ai"), 1)
    assert user.profile.voice == "aria"  # cached in this session, like in a long-lived worker
    # The user edits their profile elsewhere (the API), behind this session's back
    session.connection().execute(text(
        "UPDATE dim_profile SET voice = 'leo', tone = 'formal' WHERE user_id = :u"), {"u": user.id})

    ep = assemble_episode(session, user.id, providers)
    assert (ep.voice, ep.tone) == ("leo", "formal")


def test_greeting_transitions_and_outro_are_shared_and_use_the_small_model(
        session: Session, providers: Providers, llm: MockLLM) -> None:
    from app.database.models import PhraseClip
    seeded(session, providers)
    ai = topic(session, "ai")
    for n in range(1, 4):
        add_story(session, ai, n)
    first = make_user(session, "Ana Ruiz")
    follow(session, first, ai)
    ep1 = assemble_episode(session, first.id, providers)
    assert llm.count("phrase_write") == 1  # one call wrote every phrase incl. teasers
    assert all(c.model == providers.small_model for c in llm.calls
               if c.purpose in ("phrase_write", "clip_verify"))
    assert "Ana" in episode_transcript(session, ep1)

    # Another Ana with the same settings: no LLM call, no new recording, same audio files
    second = make_user(session, "Ana Lopez")
    follow(session, second, ai)
    tts_before = session.exec(select(CostEvent).where(CostEvent.stage == "phrase_tts")).all()
    assemble_episode(session, second.id, providers)
    assert llm.count("phrase_write") == 1
    assert session.exec(select(CostEvent).where(
        CostEvent.stage == "phrase_tts")).all() == tts_before

    # A different name only needs its own greeting
    third = make_user(session, "Ben Smith")
    follow(session, third, ai)
    assemble_episode(session, third.id, providers)
    assert llm.count("phrase_write") == 2
    # 2 greetings, 1 transition, 1 outro, 1 rundown line, 3 story teasers (shared by all three)
    assert len(session.exec(select(PhraseClip)).all()) == 8


def test_phrases_fall_back_to_english_templates_but_retry_other_languages(
        session: Session, providers: Providers) -> None:
    import pytest

    from app.pipeline.step5_phrases import PhraseSpec, ensure_phrases
    from app.providers.llm import LLMError, TransientLLMError

    def broken(req):  # type: ignore[no-untyped-def]
        raise LLMError("down")
    providers.llm.complete = broken  # type: ignore[method-assign]
    specs = [PhraseSpec("greeting:morning:ana", "greeting", name="Ana", daypart="morning")]
    out = ensure_phrases(session, providers, specs, "conversational", "en", "aria", "t")
    assert out["greeting:morning:ana"].text.startswith("Good morning, Ana. Welcome")
    with pytest.raises(TransientLLMError):
        ensure_phrases(session, providers, specs, "conversational", "es", "aria", "t")


def test_topic_frequency_and_minutes_shape_each_days_episode(session: Session,
                                                             providers: Providers) -> None:
    from datetime import date

    from app.pipeline.step1_topics import set_interests
    from app.pipeline.step4_assemble import current_profile, due_subscriptions, target_minutes

    seeded(session, providers)
    user = make_user(session)
    set_interests(session, user.id, [("technology", "daily", 5), ("world", "weekdays", 5),
                                     ("science", "weekly", 10)])
    old_science = add_story(session, topic(session, "science"), 1, hours_ago=5 * 24)
    monday, tuesday, saturday = date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 28)

    def keys(day: date) -> set[str]:
        return {topic_key(session, tid) for tid in due_subscriptions(session, user.id, day)}
    assert keys(monday) == {"technology", "world", "science"}
    assert keys(tuesday) == {"technology", "world"}
    assert keys(saturday) == {"technology"}
    profile = current_profile(session, user.id)
    assert target_minutes(due_subscriptions(session, user.id, monday), profile) == 20
    assert target_minutes(due_subscriptions(session, user.id, saturday), profile) == 5

    # weekly topics look back over the week: a 5-day-old story still counts on Monday
    found = {c.story.id for c in load_candidates(session, user, utcnow(), profile, monday)}
    assert old_science.id in found


def topic_key(session: Session, topic_id: int) -> str:
    return session.get(Topic, topic_id).key  # type: ignore[union-attr]


def test_each_topic_fills_its_own_minutes_and_filler_is_skipped() -> None:
    a, b = Topic(id=1, key="a", display_name="A"), Topic(id=2, key="b", display_name="B")
    cands = [cand(a, i, 10 - i) for i in range(1, 11)] + [cand(b, 20 + i, 1) for i in range(5)]
    cfg = pipeline_cfg().assemble
    cfg.max_per_topic, cfg.frame_overhead_seconds, cfg.transition_seconds = 12, 0, 0
    chosen = select_stories(cands, target_seconds=10 * 60, topic_seconds={1: 120.0, 2: 480.0})
    keys = [x.topic.key for x in chosen]
    assert keys.count("a") == 3  # 2 min of 40s stories, though "a" scores higher
    assert keys.count("b") == 5  # "b" keeps its own minutes; it just ran out of stories


def test_rundown_teasers_and_placeholder_name(session: Session, providers: Providers) -> None:
    from app.pipeline.step5_phrases import clean_name

    seeded(session, providers)
    ai = topic(session, "ai")
    for n in range(1, 4):
        add_story(session, ai, n)
    add_story(session, ai, 4, importance=2)  # filler: below assemble.min_importance
    user = make_user(session, "Listener")
    follow(session, user, ai)
    ep = assemble_episode(session, user.id, providers)
    text = episode_transcript(session, ep)
    assert "[Coming up] Here's what's coming up." in text
    assert len(ep.frame["rundown"]) == 4  # the line + 3 teasers
    assert len(episode_sources(session, ep)) == 3  # the importance-2 story was skipped
    assert clean_name("Listener") == "" and "Listener" not in ep.frame["intro"]
