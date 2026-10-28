"""`scripts/rehearse_orders.py`: a decided rebalance for today that the real jobs trade, on
`dev` only, and cleared afterwards."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import insert, select

from auratrade.calendar.sessions import MarketCalendar, load_calendar
from auratrade.config import Settings, TradingMode
from auratrade.db.models import PortfolioJournal, PortfolioTarget
from auratrade.execution.portfolio import due_rebalance
from auratrade.execution.store import entities
from scripts import rehearse_orders as ro
from tests.execution.conftest import TODAY, World
from tests.execution.portfolio_world import decided

NOW = datetime(2026, 10, 22, 13, 0, tzinfo=UTC)
SATELLITE = {"SPY": 0.15, "GLD": 0.05}


def rows(world: World) -> list[PortfolioTarget]:
    with world.engine.connect() as conn:
        return entities(conn, select(PortfolioTarget).order_by(PortfolioTarget.id))


def calendar(world: World) -> MarketCalendar:
    with world.engine.connect() as conn:
        return load_calendar(conn)


def test_targets_up_and_down(world: World) -> None:
    cfg = world.risk.portfolio
    up = ro.targets(1.3, SATELLITE, cfg)
    assert up == pytest.approx({"SPY": 0.65, "SSO": 0.30, "GLD": 0.05})
    down = ro.targets(0.5, SATELLITE, cfg)
    assert down == pytest.approx({"SPY": 0.45, "BIL": 0.50, "GLD": 0.05})


@pytest.mark.parametrize(
    ("level", "satellite", "match"),
    [
        (1.3, {"SPY": 0.15, "TQQQ": 0.05}, "approved"),
        (1.3, {"SPY": 0.10}, "sum"),
        (1.3, {"GLD": 0.15, "SPY": 0.05}, "above"),
        (1.6, SATELLITE, "outside"),
    ],
)
def test_targets_are_checked(
    world: World, level: float, satellite: dict[str, float], match: str
) -> None:
    with pytest.raises(ro.RehearsalError, match=match):
        ro.targets(level, satellite, world.risk.portfolio)


def test_plan_writes_a_rebalance_the_jobs_trade(world: World) -> None:
    earlier = decided(world, execution=date(2026, 10, 1))
    book = ro.plan(world.engine, world.risk, TODAY, 1.3, SATELLITE, now=NOW)
    [_, row] = rows(world)
    assert (row.kind, row.status, row.trend, row.execution_session) == (
        "REBALANCE",
        "DECIDED",
        "UP",
        TODAY,
    )
    assert row.ranking_session == date(2026, 10, 21)
    assert row.facts == {"rehearsal": True}
    with world.engine.connect() as conn:
        due = due_rebalance(conn, calendar(world), TODAY, world.risk.portfolio)
    assert due is not None
    assert due.targets == pytest.approx(book)
    assert due.previous is not None  # the earlier rebalance, as on a real E
    # A second plan replaces it: one rebalance per E.
    ro.plan(world.engine, world.risk, TODAY, 0.5, SATELLITE, now=NOW)
    [first, row] = rows(world)
    assert first.id == earlier
    assert (row.trend, row.targets) == (
        "DOWN",
        pytest.approx({"SPY": 0.45, "BIL": 0.5, "GLD": 0.05}),
    )


def test_plan_never_touches_a_real_rebalance(world: World) -> None:
    decided(world, execution=TODAY)
    with pytest.raises(ro.RehearsalError, match="real rebalance"):
        ro.plan(world.engine, world.risk, TODAY, 1.3, SATELLITE, now=NOW)
    with pytest.raises(ro.RehearsalError, match="not a session"):
        ro.plan(world.engine, world.risk, date(2026, 10, 24), 1.3, SATELLITE, now=NOW)


def test_clear_deletes_the_rehearsal_and_its_journal(world: World) -> None:
    real = decided(world, execution=date(2026, 10, 1))
    ro.plan(world.engine, world.risk, TODAY, 1.3, SATELLITE, now=NOW)
    rehearsal = rows(world)[-1].id
    with world.engine.begin() as conn:
        conn.execute(
            insert(PortfolioJournal).values(
                execution_session=TODAY, targets_id=rehearsal, summary="", decision={}
            )
        )
    assert ro.clear(world.engine) == 1
    assert [r.id for r in rows(world)] == [real]
    with world.engine.connect() as conn:
        assert conn.scalar(select(PortfolioJournal.execution_session)) is None


def settings(environment: str) -> Settings:
    return Settings(_env_file=None, environment=environment, trading_mode=TradingMode.PAPER)  # type: ignore[call-arg]


def test_main_runs_on_dev_only(world: World) -> None:
    out: list[str] = []
    code = ro.main(
        ["clear"],
        settings_factory=lambda: settings("paper"),
        engine_factory=lambda _s: world.engine,
        out=out.append,
    )
    assert code == 1
    assert out == ["refused: ENVIRONMENT is 'paper'; rehearsals run on 'dev'"]


def test_main_clears_and_refuses_bad_targets(world: World) -> None:
    out: list[str] = []
    deps = {"settings_factory": lambda: settings("dev"), "engine_factory": lambda _s: world.engine}
    assert ro.main(["clear"], out=out.append, **deps) == 0  # type: ignore[arg-type]
    assert out[-1] == "deleted 0 rehearsal rows"
    code = ro.main(
        ["plan", "--level", "1.3", "--satellite", "SPY=0.2,TQQQ=0.05"], out=out.append, **deps
    )  # type: ignore[arg-type]
    assert code == 1
    assert out[-1].startswith("refused: not on the approved list")


def test_parse_satellite() -> None:
    assert ro.parse_satellite("spy=0.15, GLD=0.05,") == {"SPY": 0.15, "GLD": 0.05}
