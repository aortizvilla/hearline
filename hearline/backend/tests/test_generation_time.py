"""Cost per episode and end-to-end generation time, stored per episode and on the dashboard."""

from sqlmodel import Session, select

from app.database.models import Episode
from app.jobs.scheduler import plan_daily, register_handlers
from app.jobs.worker import run_worker
from app.providers import Providers
from app.tracking.dashboard import Filters, _percentile, dashboard
from tests.helpers import add_story, follow, make_user, seeded, topic


def test_episodes_store_their_generation_time_and_the_dashboard_shows_it(
        engine, session: Session, providers: Providers) -> None:  # type: ignore[no-untyped-def]
    register_handlers()
    seeded(session, providers)
    for n, name in enumerate(("Ana", "Ben"), start=1):
        follow(session, make_user(session, name), topic(session, "ai"))
        add_story(session, topic(session, "ai"), n, importance=6)
    plan_daily(session, run_immediately=True)
    assert run_worker(engine, providers, stop_when_empty=True).failed == 0

    eps = session.exec(select(Episode).where(Episode.status == "ready")).all()
    assert len(eps) == 2
    for ep in eps:
        assert ep.ready_at is not None and ep.generation_seconds is not None
        assert set(ep.timing) == {"research", "clips", "assembly"}
        assert abs(ep.generation_seconds - sum(ep.timing.values())) < 0.2

    cur = dashboard(session, Filters(days=7))["current"]
    assert abs(cur["cost_per_episode"] - cur["cost"] / 2) < 1e-3
    assert abs(cur["shared_cost_per_episode"] + cur["extra_cost_per_episode"]
               - cur["cost_per_episode"]) < 1e-9
    assert cur["generation_median"] is not None and cur["generation_p95"] is not None
    assert set(cur["generation_parts"]) == {"research", "clips", "assembly"}
    series = dashboard(session, Filters(days=7))["series"]
    assert series["generation_median"][-1] is not None
    assert series["cost_per_episode"][-1] is not None


def test_percentile_is_nearest_rank() -> None:
    assert _percentile([], 50) is None
    assert _percentile([10, 20, 30, 40], 50) == 20
    assert _percentile([float(i) for i in range(1, 101)], 95) == 95
