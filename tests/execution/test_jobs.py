"""The portfolio's order jobs around the rebalance against the fake broker and the test Postgres:
`execute_open`, `post_market`, `stop_guard`, reconciliation and the kill switch (tasks 1.7, 5.2;
the rebalance itself is in `test_portfolio_orders.py`)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from auratrade.config import TradingMode
from auratrade.db.flags import TRADING_ENABLED
from auratrade.db.models import AccountSnapshot, Mismatch, OrderRow, TaxLot
from auratrade.execution.broker import LimitBuy, OrderStatus, OtoBuy, Side
from auratrade.execution.store import entities
from auratrade.jobs import execute_close, execute_open, post_market, stop_guard
from tests.execution.conftest import TODAY, World, ny
from tests.execution.portfolio_world import Market, at_close, decided, protected, stops

NEXT = date(2026, 10, 23)


def rebalanced(world: World) -> None:
    """E's first rebalance: 139 SPY and 298 SSO under its leg at 70."""
    at_close(world)
    Market(world.fake)
    decided(world)
    world.run(execute_close.make_job)


def test_an_unknown_position_is_a_mismatch_and_left_alone(world: World) -> None:
    world.fake.set_price("META", 101.0)
    world.fake.fill(world.fake.submit(OtoBuy("META", 5, 101.0, 90.0, "manual-buy")).id)
    out = world.run(execute_open.make_job)
    assert out["unknown"] == ["META"]
    assert out["problems"] == {}  # not the portfolio's: its stop is left alone
    with world.engine.connect() as conn:
        assert conn.execute(select(Mismatch.symbol)).scalar_one() == "META"
    world.run(execute_open.make_job)  # no second mismatch, no second alert
    assert sum("unknown position" in t for t in world.alerts.texts) == 1
    assert [o.symbol for o in stops(world.fake, "META")] == ["META"]


def test_orders_are_mirrored(world: World) -> None:
    rebalanced(world)
    world.run(execute_open.make_job, session=NEXT)
    with world.engine.connect() as conn:
        rows = {(r.symbol, r.purpose) for r in entities(conn, select(OrderRow))}
    assert {("SPY", "ENTRY"), ("SSO", "ENTRY"), ("SSO", "STOP_LEG")} <= rows


def test_before_the_open_a_stop_through_the_price_waits_then_sells_at_the_check_run(
    world: World,
) -> None:
    rebalanced(world)
    fake = world.fake
    [leg] = stops(fake)
    fake.delete(leg.id)
    world.clock.now = fake.now = ny(NEXT, 9, 20)
    fake.close_market()
    fake.set_price("SSO", 65.0)  # under the level of 70
    out = world.run(execute_open.make_job, session=NEXT)
    assert out["repaired"] == ["SSO"]
    assert fake.position("SSO") == 298  # nothing sold before the open (#124)

    world.clock.now = fake.now = ny(NEXT, 9, 31)
    fake.open_market()
    out = world.run(execute_open.make_job, session=NEXT)
    assert out["repaired"] == ["SSO"]
    assert fake.position("SSO") == 0
    assert protected(world)


def test_post_market_snapshot_report_and_backup(world: World) -> None:
    rebalanced(world)
    world.clock.now = ny(TODAY, 16, 20)
    world.fake.close_market()
    out = world.run(post_market.make_job)
    assert out["buys_cancelled"] == 0
    assert world.storage.exists("backup/portfolio_targets/2026-10-22.parquet")
    report = next(t for t in world.alerts.texts if "daily report" in t)
    assert "SSO 298 sh" in report
    assert "stop 70.00" in report
    assert "SPY 139 sh" in report
    assert "Stops: the leveraged fund covered" in report
    with world.engine.connect() as conn:
        (snap,) = entities(conn, select(AccountSnapshot))
    assert snap.open_positions == 2
    assert snap.drawdown == 0.0


def test_post_market_rebuilds_the_tax_lots_from_the_fills(world: World) -> None:
    rebalanced(world)
    world.clock.now = ny(TODAY, 16, 20)
    world.fake.close_market()
    spy = world.fake.prices(["SPY"])["SPY"]
    assert spy is not None
    world.fake.set_price("SPY", spy + 10.0)  # a gain of $10 a share since the buy
    world.fake.distribute("SPY", 50.0)
    out = world.run(post_market.make_job)
    assert out["tax_notes"] == []
    with world.engine.connect() as conn:
        lots = entities(conn, select(TaxLot).order_by(TaxLot.symbol, TaxLot.kind))
        (snap,) = entities(conn, select(AccountSnapshot))
    assert [(r.kind, r.symbol, r.units) for r in lots] == [
        ("INCOME", "SPY", 0.0),
        ("LOT", "SPY", 139.0),
        ("LOT", "SSO", 298.0),
    ]
    sso = world.fake.prices(["SSO"])["SSO"]
    assert sso is not None
    assert set(snap.holdings) == {"SPY", "SSO"}
    assert snap.holdings["SPY"] == {"units": 139.0, "value": pytest.approx(139 * (spy + 10.0))}
    # The SPY gain and the distribution, at 25%; SSO is at its cost.
    assert snap.tax_owed == pytest.approx(0.25 * (139 * 10.0 + 50.0))
    assert out["tax_owed"] == pytest.approx(snap.tax_owed, abs=0.01)
    report = next(t for t in world.alerts.texts if "daily report" in t)
    assert f"(${snap.tax_owed:,.2f} if everything were sold now)" in report


def test_post_market_reports_lots_that_dont_add_up(world: World) -> None:
    rebalanced(world)
    world.clock.now = ny(TODAY, 16, 20)
    world.fake.close_market()
    world.fake.split_ex_date("SSO")
    world.fake.distribute("SSO", 0.0, kind="SPLIT")
    out = world.run(post_market.make_job)
    assert out["tax_notes"] == ["2026-10-22 SPLIT in SSO: not applied to the lots"]
    report = next(t for t in world.alerts.texts if "daily report" in t)
    assert "Tax lots: CHECK (1)" in report


def test_post_market_without_the_activities_keeps_the_lots_and_reports_the_tax_unknown(
    world: World,
) -> None:
    rebalanced(world)
    world.run(post_market.make_job)
    world.fake.fail_next("activities")
    out = world.run(post_market.make_job)
    assert out["tax_lots"] is None
    assert out["tax_owed"] is None
    with world.engine.connect() as conn:
        assert len(entities(conn, select(TaxLot))) == 2  # the evening before's
        (snap,) = entities(conn, select(AccountSnapshot))
    assert snap.tax_owed is None
    report = next(t for t in reversed(world.alerts.texts) if "daily report" in t)
    assert "After the estimated tax: unknown" in report
    assert "the lots weren't rebuilt: BrokerUnavailableError" in report


def test_post_market_cancels_a_buy_left_resting(world: World) -> None:
    at_close(world)
    world.fake.submit(LimitBuy("SPY", 10, 400.0, "manual-rest"))  # under the price: rests
    world.clock.now = ny(TODAY, 16, 20)
    out = world.run(post_market.make_job)
    assert out["buys_cancelled"] == 1
    assert [o for o in world.fake.open_orders() if o.side is Side.BUY] == []


def test_post_market_dry_run_still_reports(world: World) -> None:
    out = world.run(post_market.make_job, mode=TradingMode.DRY_RUN)
    assert out["dry_run"] is True
    assert any("daily report" in t for t in world.alerts.texts)


def test_the_kill_switch_cancels_resting_buys_in_any_order_job(world: World) -> None:
    at_close(world)
    world.fake.submit(LimitBuy("SPY", 10, 400.0, "manual-rest"))
    world.set_flag(TRADING_ENABLED, False)
    world.run(execute_open.make_job)
    assert all(
        o.status is OrderStatus.CANCELED for o in world.fake.all_orders() if o.side is Side.BUY
    )
    assert any("cancelled 1 resting buy orders" in t for t in world.alerts.texts)


# --- stop_guard -----------------------------------------------------------------------------


def test_stop_guard_does_nothing_when_all_is_well(world: World) -> None:
    rebalanced(world)
    calls = len(world.fake.calls)
    assert world.run(stop_guard.make_job) == {"ok": True}
    assert world.fake.calls[calls:] == ["is_market_open", "positions", "open_orders"]


def test_stop_guard_skips_a_closed_market(world: World) -> None:
    world.fake.close_market()
    assert world.run(stop_guard.make_job) == {"skipped": "market closed"}
