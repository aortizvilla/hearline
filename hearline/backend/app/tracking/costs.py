"""Cost estimation (prices from config/pricing.yaml) and CostEvent recording."""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.config import pricing_cfg
from app.database.models import CostEvent, Episode, utcnow
from app.providers.llm import LLMResponse

log = logging.getLogger(__name__)
_warned: set[str] = set()


def llm_cost(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0,
             cache_write: int = 0, searches: int = 0, batched: bool = False,
             provider: str = "anthropic") -> float:
    """USD cost of one LLM call (tokens per 1M, searches per 1K)."""
    pricing = pricing_cfg()
    price = pricing.llm.get(model)
    if price is None:  # dated snapshot, e.g. gpt-5.4-mini-2026-03-17 -> gpt-5.4-mini
        prefixes = [k for k in pricing.llm if k != "default" and model.startswith(k)]
        price = pricing.llm[max(prefixes, key=len)] if prefixes else None
    if price is None:
        if model not in _warned:
            log.warning("No price for model %r in pricing.yaml; using `default`.", model)
            _warned.add(model)
        price = pricing.llm["default"]
    tokens = (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_read * (price.cache_read if price.cache_read is not None else price.input)
        + cache_write * (price.cache_write if price.cache_write is not None else price.input)
    ) / 1_000_000
    if batched:
        tokens *= pricing.batch_discount
    search_price = pricing.web_search_per_1k.get(provider, max(pricing.web_search_per_1k.values(),
                                                               default=0.0))
    return round(tokens + searches * search_price / 1000, 8)


def tts_cost(provider: str, chars: int) -> float:
    """USD cost of speaking `chars` characters with the named TTS provider (e.g. polly-neural)."""
    prices = pricing_cfg().tts_per_1m_chars
    price = prices.get(provider)
    if price is None:
        if provider not in _warned:
            log.warning("No TTS price for %r in pricing.yaml; using `default`.", provider)
            _warned.add(provider)
        price = prices.get("default", 0.0)
    return round(chars * price / 1_000_000, 8)


def record_llm(session: Session, stage: str, ref_id: str, resp: LLMResponse) -> CostEvent:
    u = resp.usage
    cost = llm_cost(resp.model, u.input_tokens, u.output_tokens, u.cache_read_tokens,
                    u.cache_write_tokens, u.searches, resp.batched, resp.provider or "anthropic")
    event = CostEvent(stage=stage, ref_id=ref_id, model=resp.model, input_tokens=u.input_tokens,
                      output_tokens=u.output_tokens, cache_read_tokens=u.cache_read_tokens,
                      cache_write_tokens=u.cache_write_tokens, searches=u.searches,
                      batched=resp.batched, cost_usd=cost)
    session.add(event)
    return event


def record_tts(session: Session, stage: str, ref_id: str, provider: str, chars: int) -> CostEvent:
    event = CostEvent(stage=stage, ref_id=ref_id, model=f"tts:{provider}", tts_chars=chars,
                      cost_usd=tts_cost(provider, chars))
    session.add(event)
    return event


def spent_since(session: Session, since: datetime) -> float:
    total = session.exec(
        select(func.coalesce(func.sum(CostEvent.cost_usd), 0.0)).where(
            col(CostEvent.created_at) >= since
        )
    ).one()
    return float(total or 0.0)


def spent_today(session: Session, now: datetime | None = None) -> float:
    """USD recorded since UTC midnight."""
    now = now or utcnow()
    midnight = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
    return spent_since(session, midnight)


def cost_report(session: Session, days: int, now: datetime | None = None) -> dict[str, Any]:
    """Cost by stage, per episode and per active user over the last `days` days."""
    now = now or utcnow()
    since = now - timedelta(days=days)
    rows = session.exec(
        select(CostEvent.stage, func.count(), func.sum(CostEvent.cost_usd),
               func.sum(CostEvent.input_tokens + CostEvent.cache_read_tokens
                        + CostEvent.cache_write_tokens),
               func.sum(CostEvent.output_tokens), func.sum(CostEvent.searches),
               func.sum(CostEvent.tts_chars))
        .where(col(CostEvent.created_at) >= since).group_by(CostEvent.stage)
    ).all()
    by_stage = {r[0]: {"events": r[1], "cost_usd": round(r[2] or 0, 4), "input_tokens": r[3],
                       "output_tokens": r[4], "searches": r[5], "tts_chars": r[6]}
                for r in rows}
    total = sum(v["cost_usd"] for v in by_stage.values())
    episodes = session.exec(select(func.count()).select_from(Episode).where(
        Episode.status == "ready", col(Episode.created_at) >= since)).one()
    active_users = session.exec(select(func.count(func.distinct(Episode.user_id))).where(
        Episode.status == "ready", col(Episode.created_at) >= since)).one()
    per_user_stages = {"phrase_write", "phrase_tts"}  # greeting/transitions/outro (mostly cached)
    marginal = sum(v["cost_usd"] for k, v in by_stage.items() if k in per_user_stages)
    return {
        "days": days, "by_stage": by_stage, "total_usd": round(total, 4),
        "episodes": episodes, "active_users": active_users,
        "per_episode_usd": round(total / episodes, 4) if episodes else None,
        "per_active_user_usd": round(total / active_users, 4) if active_users else None,
        "shared_usd": round(total - marginal, 4),
        "per_user_marginal_usd": round(marginal / episodes, 4) if episodes else None,
    }


def print_cost_report(report: dict[str, Any]) -> None:
    print(f"\nCosts over the last {report['days']} day(s)")
    print(f"{'stage':<15}{'events':>8}{'cost $':>11}{'in tok':>12}{'out tok':>10}"
          f"{'search':>8}{'tts chars':>11}")
    for stage, v in sorted(report["by_stage"].items(), key=lambda kv: -kv[1]["cost_usd"]):
        print(f"{stage:<15}{v['events']:>8}{v['cost_usd']:>11.4f}{v['input_tokens'] or 0:>12}"
              f"{v['output_tokens'] or 0:>10}{v['searches'] or 0:>8}{v['tts_chars'] or 0:>11}")
    print(f"{'TOTAL':<15}{'':>8}{report['total_usd']:>11.4f}")
    print(f"episodes: {report['episodes']}   active users: {report['active_users']}")
    print(f"cost per episode: ${report['per_episode_usd']}   per active user: "
          f"${report['per_active_user_usd']}")
    print(f"  shared (research + clips): ${report['shared_usd']}   new spoken phrases "
          f"(greeting/transitions/outro): ${report['per_user_marginal_usd']} per episode")
