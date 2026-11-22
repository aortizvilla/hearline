"""Step 3 (scripts): cache hits/misses, verification failure handling, local checks."""

from sqlmodel import Session, select

from app.database.models import CostEvent, StoryClip
from app.pipeline.step3_scripts import ensure_clip, ensure_clips, local_issues
from app.providers import Providers
from app.providers.mock_llm import MockLLM
from app.tracking import metrics
from tests.helpers import add_story, seeded, topic


def test_clip_cache_miss_then_hit(session: Session, providers: Providers, llm: MockLLM) -> None:
    seeded(session, providers)
    s = add_story(session, topic(session, "ai"), 1)
    clip = ensure_clip(session, s, "conversational", "en", "aria", providers)
    assert clip.verified and clip.audio_path and clip.duration_sec > 0
    assert clip.cost_estimate > 0
    calls = len(llm.calls)

    again = ensure_clip(session, s, "witty", "en", "aria", providers)  # unknown: the default
    assert again.id == clip.id and len(llm.calls) == calls
    assert metrics.snapshot() == {"clip_cache_miss": 1, "clip_cache_hit": 1}

    other = ensure_clip(session, s, "calm", "en", "aria", providers)  # different preset
    assert other.id != clip.id
    assert len(session.exec(select(StoryClip)).all()) == 2


def test_verification_failure_regenerates_once_then_excludes(
        session: Session, providers: Providers, llm: MockLLM) -> None:
    seeded(session, providers)
    s = add_story(session, topic(session, "ai"), 1)
    llm.fail_verify_story_ids = {s.id}
    clip = ensure_clip(session, s, "conversational", "en", "aria", providers)
    assert not clip.verified
    assert clip.audio_path is None  # no TTS spent on a rejected clip
    assert "not in source" in clip.verification_notes
    assert llm.count("clip_write") == 2 and llm.count("clip_verify") == 2
    stages = [e.stage for e in session.exec(select(CostEvent))]
    assert stages.count("clip_write") == 2 and "clip_tts" not in stages
    # cached as failed: not regenerated on every request
    ensure_clip(session, s, "conversational", "en", "aria", providers)
    assert llm.count("clip_write") == 2


def test_local_checks_catch_urls_markdown_and_missing_attribution(
        session: Session, providers: Providers) -> None:
    seeded(session, providers)
    s = add_story(session, topic(session, "ai"), 1)
    good = "According to Reuters, " + "officials announced development 1 today. " * 10
    assert local_issues(good, s, "en") == []
    no_figure = good.replace("development 1", "a development")
    assert any("data points" in i for i in local_issues(no_figure, s, "en"))
    stock = good + " That matters because it affects many people."
    assert any("stock line" in i for i in local_issues(stock, s, "en"))
    assert any("stock line" in i for i in local_issues(
        "Según Reuters, " + "se anunció el desarrollo 1. " * 10 + "Eso importa porque sí.",
        s, "es"))
    issues = local_issues("**Big news** at https://x.com " + "word " * 60, s, "en")
    assert any("URL" in i for i in issues)
    assert any("markdown" in i for i in issues)
    assert any("attribute" in i for i in issues)
    assert any("length" in i for i in local_issues("Reuters says hi.", s, "en"))


def test_bulk_generation_uses_batches(session: Session, providers: Providers,
                                      llm: MockLLM) -> None:
    seeded(session, providers)
    stories = [add_story(session, topic(session, "ai"), n) for n in range(3)]
    clips = ensure_clips(session, [(s, "formal", "en", "leo") for s in stories], providers,
                         batch=True)
    assert len(clips) == 3 and all(c.verified for c in clips)
    assert all(e.batched for e in session.exec(select(CostEvent)).all()
               if e.stage in ("clip_write", "clip_verify"))


def test_a_new_voice_reuses_the_checked_script_and_only_records_audio(
        session: Session, providers: Providers, llm: MockLLM) -> None:
    seeded(session, providers)
    s = add_story(session, topic(session, "ai"), 1)
    aria = ensure_clip(session, s, "conversational", "en", "aria", providers)
    writes, checks = llm.count("clip_write"), llm.count("clip_verify")

    leo = ensure_clip(session, s, "conversational", "en", "leo", providers)
    assert (llm.count("clip_write"), llm.count("clip_verify")) == (writes, checks)  # no LLM
    assert leo.script == aria.script and leo.verified
    assert leo.audio_path and leo.audio_path != aria.audio_path  # new recording
    assert metrics.snapshot()["clip_script_reused"] == 1

    ensure_clip(session, s, "conversational", "es", "leo", providers)  # new language: adapted
    assert llm.count("clip_write") == writes and llm.count("clip_adapt") == 1


def test_one_write_per_story_other_tones_and_languages_are_adapted(
        session: Session, providers: Providers, llm: MockLLM) -> None:
    seeded(session, providers)
    s = add_story(session, topic(session, "ai"), 1)
    specs = [(s, t, lang, v) for t, lang in [("formal", "es"), ("conversational", "en"),
                                             ("calm", "de")] for v in ("aria", "leo")]
    clips = ensure_clips(session, specs, providers, synthesize=False)
    assert len(clips) == 6 and all(c.verified for c in clips)
    assert llm.count("clip_write") == 1  # the base (conversational/en), from the record
    assert llm.count("clip_adapt") == 2  # formal/es and calm/de; each voice shares the words
    assert llm.count("clip_verify") == 3  # every script is fact-checked
    adapt_costs = session.exec(select(CostEvent).where(CostEvent.stage == "clip_adapt")).all()
    assert len(adapt_costs) == 2

    ensure_clip(session, s, "energetic", "fr", "aria", providers, synthesize=False)
    assert llm.count("clip_write") == 1  # later combos adapt from the stored base


def test_a_failed_base_falls_back_to_writing_from_the_record(
        session: Session, providers: Providers, llm: MockLLM) -> None:
    seeded(session, providers)
    s = add_story(session, topic(session, "ai"), 1)
    llm.fail_verify_story_ids = {s.id}
    ensure_clips(session, [(s, "conversational", "en", "aria"), (s, "formal", "es", "aria")],
                 providers, synthesize=False)
    assert llm.count("clip_adapt") == 0  # never adapt from an unverified script
    assert llm.count("clip_write") == 4  # base + rewrite, then formal/es + rewrite


def test_per_combo_strategy_writes_every_combo_from_the_record(
        session: Session, providers: Providers, llm: MockLLM, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.config import pipeline_cfg

    monkeypatch.setattr(pipeline_cfg().clips, "strategy", "per_combo")
    seeded(session, providers)
    s = add_story(session, topic(session, "ai"), 1)
    ensure_clips(session, [(s, "conversational", "en", "aria"), (s, "formal", "es", "aria")],
                 providers, synthesize=False)
    assert (llm.count("clip_write"), llm.count("clip_adapt")) == (2, 0)


def test_switching_tts_provider_records_again(session: Session, providers: Providers) -> None:
    from dataclasses import replace

    from app.providers.tts import MockTTSProvider

    seeded(session, providers)
    s = add_story(session, topic(session, "ai"), 1)
    old = ensure_clip(session, s, "conversational", "en", "aria", providers)
    assert old.audio_path and "/mock/" in f"/{old.audio_path}"

    other = MockTTSProvider(8000)
    other.name = "polly-neural"
    new = ensure_clip(session, s, "conversational", "en", "aria", replace(providers, tts=other))
    assert new.id == old.id and new.audio_path.startswith("clips/polly-neural/")  # type: ignore[union-attr]

