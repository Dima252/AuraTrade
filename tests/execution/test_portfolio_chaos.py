"""Chaos tests of the portfolio's orders (execution plan task 5.1, "Accept"): a crash between the
stop reduction and the sale, a partly filled OTO, a rejected stop with the price through it, a
split on the leveraged fund, the halt's sale under the kill switch, a rebalance killed after a
buy and run again; and the order layer's own (task 1.7): two jobs at once, a 500, a dropped
connection.

Each ends with the order layer's two properties (README §5.6, CLAUDE.md §3.5):
- **the leveraged fund is protected**: its live GTC stops cover its full quantity at or above
  its level, nothing is partly filled or working, and no other fund carries a stop
- **no duplicates**: nothing bought twice, no stops for more shares than are held
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import date, timedelta

import pytest
from sqlalchemy import insert, select

from auratrade.db.models import CorporateAction, Mismatch
from auratrade.execution.broker import BrokerError, OrderType, PriceSource, Side
from auratrade.execution.lock import Holder
from auratrade.execution.store import entities
from auratrade.jobs import execute_close, execute_open, stop_guard
from auratrade.sim.fake_broker import ProcessKilled
from tests.execution.conftest import World, ny
from tests.execution.portfolio_world import (
    Market,
    at_close,
    decided,
    no_duplicates,
    protected,
    stop_levels,
    stops,
    target_row,
)

NEXT = date(2026, 10, 23)
LOWER = {"SPY": 0.9, "SSO": 0.1}


def held(world: World) -> tuple[int, int]:
    return world.fake.position("SPY"), world.fake.position("SSO")


def bought_from_cash(world: World) -> Market:
    """E on TODAY: 139 SPY and 298 SSO, the SSO under its OTO leg at 70."""
    at_close(world)
    market = Market(world.fake)
    decided(world)
    world.run(execute_close.make_job)
    assert held(world) == (139, 298)
    return market


def lower_target_tomorrow(world: World) -> int:
    at_close(world, NEXT)
    return decided(world, LOWER, execution=NEXT)


def assert_sale_finished(world: World, row: int) -> None:
    """SSO down to 100 shares under one standalone stop at its level."""
    world.run(execute_close.make_job, session=NEXT)
    assert world.fake.position("SSO") == 100
    [stop] = stops(world.fake)
    assert (stop.qty, stop.stop_price) == (100, 70.0)
    assert target_row(world, row).executed_at is not None
    assert protected(world)
    assert no_duplicates(world.fake)


# --- a crash between the stop reduction and the sale ---------------------------------------


def test_killed_after_the_leg_was_cancelled_stop_guard_covers_the_shares(world: World) -> None:
    bought_from_cash(world)
    row = lower_target_tomorrow(world)
    # The sale first swaps the OTO leg for a standalone stop: cancel, then place (#113).
    world.fake.kill_after("cancel")
    with pytest.raises(ProcessKilled):
        world.run(execute_close.make_job, session=NEXT)
    assert stops(world.fake) == []
    out = world.run(stop_guard.make_job)
    assert out["repaired"] == ["SSO"]
    assert protected(world)
    assert_sale_finished(world, row)


def test_killed_after_the_stop_was_reduced_stop_guard_covers_the_rest(world: World) -> None:
    bought_from_cash(world)
    [leg] = stops(world.fake)
    world.fake.delete(leg.id)
    world.run(stop_guard.make_job)  # one standalone stop for the 298
    row = lower_target_tomorrow(world)
    world.fake.kill_after("replace")
    with pytest.raises(ProcessKilled):
        world.run(execute_close.make_job, session=NEXT)
    assert [s.qty for s in stops(world.fake)] == [100]  # reduced; 198 free and unprotected
    world.run(stop_guard.make_job)
    assert protected(world)
    assert_sale_finished(world, row)


def test_killed_with_the_sale_working_stop_guard_cancels_it_and_covers(world: World) -> None:
    market = bought_from_cash(world)
    row = lower_target_tomorrow(world)
    market.off.add("SSO")
    world.fake.kill_after("submit", skip=1)  # the standalone stop goes in, then the sale
    with pytest.raises(ProcessKilled):
        world.run(execute_close.make_job, session=NEXT)
    working = [o for o in world.fake.open_orders() if o.symbol == "SSO" and not o.is_sell_stop]
    assert [(o.side, o.qty) for o in working] == [(Side.SELL, 198)]
    out = world.run(stop_guard.make_job)
    assert "STRAY_SELL" in out["problems"]["SSO"]
    assert protected(world)
    market.off.clear()
    assert_sale_finished(world, row)


# --- a partly filled OTO --------------------------------------------------------------------


@pytest.mark.parametrize("leg", ["cancel", "activate"])
def test_a_partly_filled_oto_is_covered_and_the_rest_bought_on_the_retry(
    world: World, leg: str
) -> None:
    world.fake.partial_cancel_leg = leg  # type: ignore[assignment]
    at_close(world)
    market = Market(world.fake, partial={"SSO": 100})
    row = decided(world)
    out = world.run(execute_close.make_job)
    assert out["rebalance"]["buys"]["SSO"]["bought"] == 100
    assert not out["rebalance"]["finished"]
    assert world.fake.position("SSO") == 100
    assert sum(s.qty for s in stops(world.fake)) == 100
    assert protected(world)

    market.partial.clear()
    at_close(world, NEXT)
    world.run(execute_close.make_job, session=NEXT)
    assert held(world) == (139, 298)
    assert sum(s.remaining_qty for s in stops(world.fake)) == 298
    assert target_row(world, row).executed_at is not None
    assert protected(world)
    assert no_duplicates(world.fake)


def test_killed_while_the_oto_was_partly_filled_stop_guard_protects_it(world: World) -> None:
    at_close(world)
    Market(world.fake, partial={"SSO": 100})
    decided(world)
    # SPY's buy fills at once; SSO's is waited on (polled), and the job dies there.
    world.fake.kill_after("get_order", skip=0)
    with pytest.raises(ProcessKilled):
        world.run(execute_close.make_job)
    partly = [o for o in world.fake.open_orders() if o.symbol == "SSO" and o.side is Side.BUY]
    assert [o.filled_qty for o in partly] == [100]
    out = world.run(stop_guard.make_job)
    assert "PARTIAL_BUY" in out["problems"]["SSO"]
    assert world.fake.position("SSO") == 100
    assert protected(world)
    assert no_duplicates(world.fake)


# --- a rejected stop with the price through it ----------------------------------------------


class StalePrices:
    """The decision price the run saw, while the market has moved on."""

    def __init__(self, inner: PriceSource, seen: dict[str, float]) -> None:
        self.inner, self.seen = inner, seen

    def prices(self, symbols: object) -> dict[str, float | None]:
        out = self.inner.prices(symbols)  # type: ignore[arg-type]
        return {s: self.seen.get(s, p) for s, p in out.items()}


def test_a_stop_rejected_with_the_price_through_it_sells_at_once(world: World) -> None:
    bought_from_cash(world)
    at_close(world, NEXT)
    decided(world, execution=NEXT)
    # The run prices SSO at 120 (stop 84), but it trades at 80: the broker refuses the stop.
    world.fake.set_price("SSO", 80.0)
    prices = StalePrices(world.fake, {"SSO": 120.0})
    spec = execute_close.make_job(lambda _ctx: replace(world.deps(), prices=prices))
    spec.run(world.context(spec.name, session=NEXT))
    assert world.fake.position("SSO") == 0
    sells = [o for o in world.fake.all_orders() if o.symbol == "SSO" and o.side is Side.SELL]
    assert sells[-1].type is OrderType.MARKET  # the marketable limit, re-priced once, then market
    assert stops(world.fake) == []
    assert any("SSO is through its stop level: selling 298" in t for t in world.alerts.texts)
    assert protected(world)
    assert no_duplicates(world.fake)


def test_a_gap_through_the_level_with_the_stop_gone_sells_at_once(world: World) -> None:
    bought_from_cash(world)
    [leg] = stops(world.fake)
    world.fake.delete(leg.id)
    world.fake.set_price("SSO", 60.0)  # below the 70 level: no stop can be placed
    world.run(stop_guard.make_job)
    assert world.fake.position("SSO") == 0
    assert protected(world)
    assert no_duplicates(world.fake)


# --- a split on the leveraged fund ----------------------------------------------------------


def test_a_split_scales_the_level_and_replaces_the_stop_without_selling(world: World) -> None:
    bought_from_cash(world)
    fake = world.fake
    with world.engine.begin() as conn:
        conn.execute(
            insert(CorporateAction).values(
                symbol="SSO",
                action_type="SPLIT",
                ex_date=NEXT,
                ratio=2.0,
                source="test",
                source_id="sso-split",
            )
        )
    fake.close_market()
    fake.split_ex_date("SSO")  # 04:00: Alpaca rejects the resting stops (spike 0.3b)
    fake.set_price("SSO", 50.0)  # the new shares' price; paper keeps 298 old ones (#116)
    world.clock.now = fake.now = ny(NEXT, 9, 20)
    world.run(execute_open.make_job, session=NEXT)

    assert stop_levels(world) == [("ENTRY", 70.0), ("SPLIT", 35.0)]
    [stop] = stops(fake)
    assert (stop.qty, stop.stop_price) == (298, 35.0)
    assert fake.position("SSO") == 298
    assert not [o for o in fake.all_orders() if o.side is Side.SELL and not o.is_sell_stop]
    with world.engine.connect() as conn:
        [mismatch] = entities(conn, select(Mismatch).where(Mismatch.symbol == "SSO"))
    assert mismatch.kind == "CORPORATE_ACTION"
    assert any("split 2:1 in SSO" in t for t in world.alerts.texts)

    world.clock.now = fake.now = ny(NEXT, 9, 31)
    fake.open_market()
    world.run(execute_open.make_job, session=NEXT)
    assert fake.position("SSO") == 298  # the check run doesn't read the split as a fall
    assert protected(world)


# --- the halt's sale under the kill switch --------------------------------------------------


def test_the_halts_sale_goes_through_the_kill_switch(world: World) -> None:
    bought_from_cash(world)
    at_close(world, NEXT)
    world.set_flag("trading_enabled", False)
    world.set_flag("halt_drawdown", True)
    out = world.run(execute_close.make_job, session=NEXT)
    assert out["halt_sale"]["sale"]["sold"] == 298
    assert held(world) == (139 + 59, 0)  # the proceeds into the market fund, as the halt says
    assert stops(world.fake) == []
    assert protected(world)
    assert no_duplicates(world.fake)


def test_the_halts_proceeds_stay_in_cash_while_the_market_fund_is_mismatched(
    world: World,
) -> None:
    bought_from_cash(world)
    at_close(world, NEXT)
    with world.engine.begin() as conn:
        conn.execute(insert(Mismatch).values(kind="RECONCILIATION", symbol="SPY"))
    world.set_flag("halt_drawdown", True)
    out = world.run(execute_close.make_job, session=NEXT)
    assert out["halt_sale"]["sale"]["sold"] == 298
    assert "buy" not in out["halt_sale"]
    assert held(world) == (139, 0)


# --- a rebalance killed after a buy and run again -------------------------------------------


@pytest.mark.parametrize("filled", [True, False])
def test_a_rebalance_killed_after_a_buy_buys_once_run_again(world: World, filled: bool) -> None:
    at_close(world)
    market = Market(world.fake)
    if not filled:
        market.off.add("SPY")
    row = decided(world)
    world.fake.kill_after("submit")  # SPY's buy, the first
    with pytest.raises(ProcessKilled):
        world.run(execute_close.make_job)
    market.off.clear()
    world.run(execute_close.make_job)
    assert held(world) == (139, 298)
    spy_buys = [o for o in world.fake.all_orders() if o.symbol == "SPY" and o.side is Side.BUY]
    assert sum(o.filled_qty for o in spy_buys) == 139
    assert target_row(world, row).executed_at is not None
    assert protected(world)
    assert no_duplicates(world.fake)


def test_a_sale_rejected_by_the_broker_restores_the_stop(world: World) -> None:
    bought_from_cash(world)
    row = lower_target_tomorrow(world)
    world.fake.fail_next("submit", BrokerError("rejected", status=422), skip=1)
    out = world.run(execute_close.make_job, session=NEXT)
    assert out["rebalance"]["sales"]["SSO"]["status"] == "UNFILLED"
    assert world.fake.position("SSO") == 298
    assert protected(world)
    assert target_row(world, row).executed_at is None  # owed to the retry
    at_close(world, date(2026, 10, 26))
    world.run(execute_close.make_job, session=date(2026, 10, 26))
    assert world.fake.position("SSO") == 100
    assert protected(world)


# --- two order jobs at once, a 500, a dropped connection (task 1.7's, on the funds) -----------


def test_a_second_order_job_waits_for_the_lock(world: World) -> None:
    bought_from_cash(world)
    finished: list[dict[str, object]] = []
    with world.lock.hold("execute_close"):
        calls = len(world.fake.calls)
        runner = threading.Thread(
            target=lambda: finished.append(world.run(execute_open.make_job)), daemon=True
        )
        runner.start()
        time.sleep(0.6)
        assert runner.is_alive()
        assert not finished
        assert len(world.fake.calls) == calls  # it hasn't touched the broker
    runner.join(timeout=10)
    assert finished
    assert finished[0]["problems"] == {}


def test_stop_guard_skips_while_another_job_holds_the_lock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    bought_from_cash(world)
    [leg] = stops(world.fake)
    world.fake.delete(leg.id)
    with world.lock.hold("execute_close"):
        out = world.run(stop_guard.make_job)
        assert out["skipped"] == "order lock held"
        holder = world.lock.holder()
        assert holder is not None
        assert holder.application_name == "auratrade-lock:execute_close"
        assert not [t for t in world.alerts.texts if "has been held" in t]
        stuck = Holder(holder.application_name, timedelta(minutes=30))
        monkeypatch.setattr(world.lock, "holder", lambda: stuck)
        world.run(stop_guard.make_job)
        assert [t for t in world.alerts.texts if "has been held" in t]
    assert world.lock.__class__.holder(world.lock) is None
    world.run(stop_guard.make_job)
    assert protected(world)


def test_a_dropped_connection_on_a_repair_stop_places_one_stop(world: World) -> None:
    bought_from_cash(world)
    [leg] = stops(world.fake)
    world.fake.delete(leg.id)
    world.fake.drop_next("submit")
    world.run(stop_guard.make_job)
    assert [o.qty for o in stops(world.fake)] == [298]
    assert protected(world)
    assert no_duplicates(world.fake)


def test_a_500_on_every_attempt_fails_the_run_and_the_next_one_repairs(world: World) -> None:
    bought_from_cash(world)
    [leg] = stops(world.fake)
    world.fake.delete(leg.id)
    for _ in range(world.risk.execution.call_attempts):
        world.fake.fail_next("submit")
    out = world.run(stop_guard.make_job)
    assert out["still_unprotected"] == ["SSO"]
    assert [t for t in world.alerts.texts if "could not place a stop" in t]
    world.run(stop_guard.make_job)
    assert protected(world)
    assert no_duplicates(world.fake)


def test_the_sale_through_the_level_puts_its_proceeds_in_the_cash_fund(world: World) -> None:
    bought_from_cash(world)
    [leg] = stops(world.fake)
    world.fake.delete(leg.id)
    at_close(world, NEXT)
    world.fake.set_price("SSO", 65.0)  # under the level of 70: the invariant sells at once
    out = world.run(execute_close.make_job, session=NEXT)
    assert out["repaired"] == ["SSO"]
    assert world.fake.position("SSO") == 0
    # The run that repaired blocks increases: the proceeds wait in cash for a clean run.
    proceeds = out["stop_proceeds"]
    assert proceeds["stopped"] == 298
    assert proceeds["skipped"].endswith("the stop invariant needed repair")
    later = date(2026, 10, 26)
    at_close(world, later)
    out = world.run(execute_close.make_job, session=later)
    assert world.fake.position("BIL") == out["stop_proceeds"]["shares"] > 0
    assert protected(world)


def test_in_dry_run_an_order_call_never_reaches_the_broker(world: World) -> None:
    from auratrade.execution.runtime import DryRunViolationError, ReadOnlyBroker

    broker = ReadOnlyBroker(world.fake)
    assert broker.positions() == []
    for name in ("submit", "replace", "cancel"):
        with pytest.raises(DryRunViolationError, match=name):
            getattr(broker, name)
