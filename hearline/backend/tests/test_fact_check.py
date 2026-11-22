"""Step 3 fact-check: evidence and attribution re-checked in code; rejections get a 2nd look."""

import json

from sqlmodel import Session

from app.database.models import Story
from app.pipeline.step3_scripts import ensure_clip, judge
from app.providers import Providers
from app.providers.mock_llm import MockLLM
from tests.helpers import add_story, seeded, topic

FBI = Story(topic_id=1, canonical_url="https://apnews.com/article/x", source_name="AP News",
            headline="FBI investigating apparent hack of its key criminal justice database",
            summary="The AP says the FBI is investigating an apparent hack of its key national "
                    "criminal justice database.",
            why_it_matters="A breach in a major law-enforcement database could affect "
                           "sensitive information used by police and prosecutors nationwide.",
            published_at=__import__("datetime").datetime(2026, 11, 21))


def claim(text: str, evidence: str, field: str, attributed: bool) -> dict:  # type: ignore[type-arg]
    return {"claim": text, "evidence": evidence, "field": field, "attributed": attributed,
            "supported": True}


def verdict(*claims: dict) -> dict:  # type: ignore[type-arg]
    return {"claims": list(claims), "attribution_ok": True, "supported": True, "issues": []}


def test_the_fbi_attribution_slip_is_caught_even_if_the_checker_misses_it() -> None:
    slip = claim("AP reports the system is a major law-enforcement database",
                 "A breach in a major law-enforcement database", "why_it_matters", True)
    ok, issues = judge(verdict(slip), FBI)  # the checker said "supported": code overrules
    assert not ok and "only in the podcast's own framing" in issues[0]

    framing = {**slip, "claim": "That matters for police nationwide", "attributed": False}
    reported = claim("AP says the FBI is investigating a hack",
                     "the FBI is investigating an apparent hack", "summary", True)
    assert judge(verdict(reported, framing), FBI) == (True, [])  # framing in own voice is fine


def test_made_up_evidence_fails() -> None:
    invented = claim("The FBI arrested two suspects", "two suspects were arrested", "summary",
                     False)
    ok, issues = judge(verdict(invented), FBI)
    assert not ok and "no evidence in the story record" in issues[0]
    quoted = claim("AP says it is a national database",
                   "the FBI is investigating ... national criminal justice database", "summary",
                   True)
    assert judge(verdict(quoted), FBI)[0]  # "a ... b" quotes are matched part by part


def test_a_wrong_rejection_is_overturned_by_the_second_check(
        session: Session, providers: Providers, llm: MockLLM, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seeded(session, providers)
    story = add_story(session, topic(session, "ai"), 1)
    monkeypatch.setattr(llm, "_clip_verify", lambda req: json.dumps(
        {"claims": [], "attribution_ok": True, "supported": False, "issues": ["too strict"]}))
    monkeypatch.setattr(llm, "_clip_verify_second", lambda req: json.dumps(
        {"claims": [], "attribution_ok": True, "supported": True, "issues": []}))
    clip = ensure_clip(session, story, "conversational", "en", "aria", providers,
                       synthesize=False)
    assert clip.verified and llm.count("clip_verify_second") == 1
    assert llm.count("clip_write") == 1  # no rewrite needed
    assert next(c for c in llm.calls if c.purpose == "clip_verify_second").model == (
        providers.writer_model)


def test_when_both_checks_reject_the_script_is_rewritten(
        session: Session, providers: Providers, llm: MockLLM) -> None:
    seeded(session, providers)
    story = add_story(session, topic(session, "ai"), 1)
    llm.fail_verify_story_ids = {story.id}  # both checks reject
    clip = ensure_clip(session, story, "conversational", "en", "aria", providers,
                       synthesize=False)
    assert not clip.verified and llm.count("clip_write") == 2  # rewritten once, still rejected
    assert llm.count("clip_verify_second") == 2  # each rejection got a second look


def test_the_second_check_cannot_overturn_an_objection_it_upholds() -> None:
    upheld = {**verdict(), "issue_rulings": [
        {"issue": "attributes why_it_matters to Axios", "valid": True, "reason": "it does"}]}
    ok, issues = judge(upheld, FBI)
    assert not ok and issues[0].startswith("confirmed: attributes why_it_matters")
    dismissed = {**verdict(), "issue_rulings": [
        {"issue": "ordinary investors framing", "valid": False, "reason": "own voice"}]}
    assert judge(dismissed, FBI) == (True, [])

