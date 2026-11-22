"""Cost accounting from config/pricing.yaml."""

from sqlmodel import Session

from app.config import pricing_cfg
from app.database.models import CostEvent, Episode
from app.providers.llm import LLMResponse, Usage
from app.tracking.costs import cost_report, llm_cost, record_llm, spent_today, tts_cost


def test_llm_cost_uses_pricing_config() -> None:
    p = pricing_cfg().llm["claude-opus-5-5"]
    cost = llm_cost("claude-opus-5-5", 1_000_000, 100_000, cache_read=1_000_000, searches=10)
    expected = p.input + p.output / 10 + p.cache_read + 10 * pricing_cfg().web_search_per_1k[
        "anthropic"] / 1000
    assert abs(cost - expected) < 1e-9
    batched = llm_cost("claude-opus-5-5", 1_000_000, 0, batched=True)
    assert abs(batched - p.input * pricing_cfg().batch_discount) < 1e-9
    assert llm_cost("unknown-model", 1_000_000, 0) == pricing_cfg().llm["default"].input


def test_tts_cost() -> None:
    assert tts_cost("mock", 1_000_000) == pricing_cfg().tts_per_1m_chars["mock"]
    assert tts_cost("say", 1_000_000) == 0


def test_events_and_report(session: Session) -> None:
    resp = LLMResponse("x", "claude-haiku-5-5", Usage(input_tokens=1000, output_tokens=500),
                       provider="anthropic")
    ev = record_llm(session, "clip_verify", "story:1", resp)
    session.add(CostEvent(stage="frame", ref_id="episode:1", cost_usd=0.01))
    session.add(Episode(user_id=1, status="ready"))
    session.commit()
    assert ev.cost_usd > 0
    assert abs(spent_today(session) - (ev.cost_usd + 0.01)) < 1e-9
    report = cost_report(session, days=1)
    assert report["episodes"] == 1 and report["by_stage"]["clip_verify"]["events"] == 1
    assert abs(report["per_episode_usd"] - round(ev.cost_usd + 0.01, 4)) < 1e-4
