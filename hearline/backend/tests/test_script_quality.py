"""Episode quality rules that are enforced, not just asked for: one subject per episode,
clear fixed transitions, a neutral rundown, background that names no source and only follows
its story, and the research relevance check dropping incidental background terms."""

from datetime import date

from sqlmodel import Session

from app.database.models import Topic
from app.pipeline.step2_research import RawStory, _on_topic
from app.pipeline.step3_scripts import local_issues
from app.pipeline.step4_assemble import same_subject
from app.pipeline.step5_phrases import _write, end_sentence, episode_phrases, rundown_phrases
from app.providers import Providers
from tests.helpers import add_story, seeded, topic


def test_two_headlines_about_one_subject_are_one_subject(session: Session,
                                                         providers: Providers) -> None:
    seeded(session, providers)
    film = topic(session, "ai")
    bond = add_story(session, film, 1, headline="James Bond, el agente 007 con licencia "
                     "para recaudar")
    bond.summary = ("El anuncio de Robert Pattinson como próximo 007 pone el foco en el "
                    "negocio de James Bond.")
    again = add_story(session, film, 2, headline="Robert Pattinson reaparece tras ser elegido "
                      "como el nuevo James Bond")
    again.summary = "Robert Pattinson reapareció en público tras ser anunciado como James Bond."
    other = add_story(session, film, 3, headline="RTVE Play estrena cine gratis esta semana")
    other.summary = "RTVE reúne los estrenos gratuitos de la semana del 23 al 29 de noviembre."
    vec = providers.embedder.embed_one("James Bond Robert Pattinson")
    bond.embedding = again.embedding = vec
    other.embedding = providers.embedder.embed_one("RTVE Play cine gratis")
    assert same_subject(bond, again)
    assert not same_subject(bond, other)


def test_transitions_are_fixed_clear_and_in_the_listener_language() -> None:
    cine = Topic(id=1, key="film-tv", display_name="Film & TV",
                 labels={"en": "Film", "es": "Cine"})
    hist = Topic(id=2, key="history", display_name="History", labels={"es": "Historia"})
    _, transitions, _ = episode_phrases("Sofía", 7, [cine, hist], date(2026, 11, 22), "es")
    assert [t.key for t in transitions] == ["transition:first:film-tv",
                                            "transition:next:history"]
    texts = _write(None, None, transitions, "conversational", "es", "t")  # type: ignore[arg-type]
    assert texts == {"transition:first:film-tv": "Empezamos con Cine.",
                     "transition:next:history": "Pasamos ahora a Historia."}


def test_rundown_is_neutral_and_teasers_end_their_sentence(session: Session,
                                                          providers: Providers) -> None:
    seeded(session, providers)
    stories = [add_story(session, topic(session, "ai"), n) for n in (1, 2)]
    rundown = rundown_phrases(stories)[0]
    assert _write(None, None, [rundown], "conversational", "es", "t") == {  # type: ignore[arg-type]
        "rundown:fixed": "Esto es lo que te contamos hoy."}
    assert end_sentence("Estrenos gratis en RTVE Play") == "Estrenos gratis en RTVE Play."
    assert end_sentence("¿Qué pasó?") == "¿Qué pasó?"


def test_background_never_names_its_source(session: Session, providers: Providers) -> None:
    seeded(session, providers)
    s = add_story(session, topic(session, "ai"), 1)
    s.status, s.source_name = "explainer", "Wikipedia"
    text = "Some context: " + "a European rocket built for heavy loads. " * 10
    assert local_issues(text, s, "en") == []
    assert any("Wikipedia" in i for i in local_issues(
        "According to Wikipedia, " + "a European rocket. " * 15, s, "en"))


def test_relevance_check_drops_incidental_background_terms(session: Session,
                                                           providers: Providers) -> None:
    seeded(session, providers)
    raw = RawStory.model_validate({
        "headline": "New Bond casting", "summary": "Robert Pattinson is the new James Bond.",
        "why_it_matters": "", "source_name": "Reuters", "url": "https://reuters.com/a",
        "published_date": "2026-11-22", "importance": 6,
        "background_terms": ["Eon Productions", "Aston Martin incidental"]})
    keep = _on_topic(session, topic(session, "ai"), providers, [raw])
    assert keep == {0} and raw.background_terms == ["Eon Productions"]


def test_script_tells_the_news_and_adds_a_curiosity(session: Session,
                                                     providers: Providers) -> None:
    seeded(session, providers)
    s = add_story(session, topic(session, "ai"), 1)
    s.background = [{"term": "Eon Productions", "language": "en", "summary": (
        "Eon Productions has produced the James Bond films since 1962, starting with Dr. No "
        "and Sean Connery in the lead role.")}]
    base = "According to Reuters, " + "officials confirmed a recall notice worth 1 million. " * 8
    curiosity = " For perspective, the Bond films have been produced since 1962, when Sean " \
                "Connery starred in Dr. No."
    assert local_issues(base + curiosity, s, "en") == []
    assert any("curiosity" in i for i in local_issues(base, s, "en"))
    meta = base.replace("officials confirmed", "the article reviews how officials confirmed", 1)
    assert any("describes the article" in i for i in local_issues(meta + curiosity, s, "en"))
    assert any("describes the article" in i for i in local_issues(
        "Según Reuters, el texto repasa " + "una retirada de 1 millón. " * 10, s, "es"))
    # background only in another language: no curiosity can be checked, none demanded
    s.background[0]["language"] = "es"
    assert not any("curiosity" in i for i in local_issues(base, s, "en"))


def test_cached_script_breaking_a_newer_rule_is_rewritten(session: Session,
                                                          providers: Providers) -> None:
    from sqlmodel import select

    from app.database.models import StoryClip
    from app.pipeline.step3_scripts import ensure_clips
    seeded(session, providers)
    s = add_story(session, topic(session, "ai"), 1)
    old = StoryClip(story_id=s.id, tone="conversational", language="en", voice="aria",
                    verified=True, script="According to Reuters, the article reviews how "
                    "officials confirmed a recall notice worth 1 million. " * 5)
    session.add(old)
    session.commit()
    clip = ensure_clips(session, [(s, "conversational", "en", "aria")], providers)[0]
    assert clip.verified and "the article reviews" not in clip.script
    assert len(session.exec(select(StoryClip)).all()) == 1  # the stale one is gone
