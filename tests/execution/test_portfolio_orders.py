"""The portfolio's trades through `execute_close` (task 5.1) on the fake broker and the test
Postgres: the rebalance, its retry, the stop and its raise, the blocks, the halt's sale."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import pytest
from sqlalchemy import insert

from auratrade.config import TradingMode
from auratrade.db.models import Mismatch
from auratrade.execution.broker import OrderClass, OrderType, Side, TimeInForce
from auratrade.jobs import execute_close, stop_guard
from tests.execution.conftest import TODAY, World
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
LATER = date(2026, 11, 2)


def first_rebalance(world: World) -> int:
    at_close(world)
    Market(world.fake)
    row = decided(world)
    world.run(execute_close.make_job)
    return row


def first_rebalance_market(world: World) -> Market:
    at_close(world)
    market = Market(world.fake)
    decided(world)
    world.run(execute_close.make_job)
    return market


def test_the_first_rebalance_buys_the_targets_from_cash(world: World) -> None:
    row = first_rebalance(world)
    fake = world.fake
    assert (fake.position("SPY"), fake.position("SSO")) == (139, 298)
    [leg] = stops(fake)
    assert (leg.qty, leg.stop_price, leg.time_in_force, leg.order_class) == (
        298,
        70.0,
        TimeInForce.GTC,
        OrderClass.OTO,
    )
    buys = [o for o in fake.all_orders() if o.side is Side.BUY]
    assert all(o.type is OrderType.LIMIT for o in buys)  # never a market buy
    assert {o.symbol: o.order_class for o in buys} == {
        "SPY": OrderClass.SIMPLE,
        "SSO": OrderClass.OTO,
    }
    assert stop_levels(world) == [("ENTRY", 70.0)]
    assert protected(world)
    assert no_duplicates(fake)
    assert fake.cash >= 0  # no margin
    target = target_row(world, row)
    assert target.executed_at is not None
    [entry] = target.executions
    assert entry["finished"]
    assert entry["mode"] == "paper"
    assert entry["buys"]["SSO"]["bought"] == 298


def test_a_second_run_the_same_day_does_nothing(world: World) -> None:
    first_rebalance(world)
    before = len(world.fake.all_orders())
    out = world.run(execute_close.make_job)
    assert "rebalance" not in out
    assert len(world.fake.all_orders()) == before


def test_dry_run_plans_and_sends_nothing(world: World) -> None:
    at_close(world)
    row = decided(world)
    out = world.run(execute_close.make_job, mode=TradingMode.DRY_RUN)
    assert world.fake.all_orders() == []
    planned = {m["symbol"]: (m["action"], m["qty"]) for m in out["rebalance"]["planned"]}
    assert planned == {"SPY": ("BUY", 139), "SSO": ("BUY", 298)}
    assert out["rebalance"]["level"] == 70.0
    target = target_row(world, row)
    assert target.executed_at is None
    assert target.executions[0]["mode"] == "dry_run"
    assert stop_levels(world) == []


def test_the_next_e_raises_the_stop_and_leaves_holdings_inside_the_band(world: World) -> None:
    first_rebalance(world)
    fake = world.fake
    at_close(world, LATER)
    fake.set_price("SSO", 120.0)
    decided(world, execution=LATER)
    out = world.run(execute_close.make_job, session=LATER)
    assert {m["symbol"]: m["why"] for m in out["rebalance"]["planned"]} == {
        "SPY": "WITHIN_BAND",
        "SSO": "WITHIN_BAND",
    }
    assert [o for o in fake.all_orders() if o.side is Side.BUY and o.created_at == fake.now] == []
    [stop] = stops(fake)
    assert (stop.qty, stop.stop_price) == (298, 84.0)
    assert stop_levels(world) == [("ENTRY", 70.0), ("RAISE", 84.0)]
    assert protected(world)


def test_a_lower_target_sells_the_leveraged_fund_through_the_sell_sequence(world: World) -> None:
    first_rebalance(world)
    fake = world.fake
    at_close(world, LATER)
    decided(world, {"SPY": 0.9, "SSO": 0.1}, execution=LATER)
    world.run(execute_close.make_job, session=LATER)
    # 10% of ~100,000 at 100: 100 shares kept, under one standalone stop at the level.
    assert fake.position("SSO") == 100
    [stop] = stops(fake)
    assert (stop.qty, stop.stop_price, stop.is_leg) == (100, 70.0, False)
    assert fake.position("SPY") > 139
    assert protected(world)
    assert no_duplicates(fake)
    assert fake.cash >= 0


def test_the_kill_switch_lets_sales_go_and_holds_buys_until_the_retry(world: World) -> None:
    at_close(world)
    Market(world.fake)
    fake = world.fake
    fake._book("QQQ", Side.BUY, 100, 400.0)
    row = decided(world)
    world.set_flag("trading_enabled", False)
    out = world.run(execute_close.make_job)
    assert fake.position("QQQ") == 0  # a sale toward a lower target
    assert (fake.position("SPY"), fake.position("SSO")) == (0, 0)
    assert "kill switch on" in out["rebalance"]["blocks"]
    assert target_row(world, row).executed_at is None

    world.set_flag("trading_enabled", True)
    at_close(world, NEXT)
    out = world.run(execute_close.make_job, session=NEXT)
    assert out["rebalance"]["retry"] == 1
    assert out["rebalance"]["finished"]
    assert (fake.position("SPY"), fake.position("SSO")) == (139, 298)
    assert target_row(world, row).executed_at is not None
    assert protected(world)


def test_a_rebalance_unfinished_after_its_retry_is_dropped(world: World) -> None:
    at_close(world)
    row = decided(world)
    world.set_flag("trading_enabled", False)
    world.run(execute_close.make_job)
    at_close(world, NEXT)
    world.run(execute_close.make_job, session=NEXT)
    assert any("didn't finish on its last try" in t for t in world.alerts.texts)
    at_close(world, date(2026, 10, 26))
    out = world.run(execute_close.make_job, session=date(2026, 10, 26))
    assert "rebalance" not in out
    assert len(target_row(world, row).executions) == 2


def test_an_undecided_rebalance_holds(world: World) -> None:
    at_close(world)
    decided(world, status="ALLOCATED")
    out = world.run(execute_close.make_job)
    assert "rebalance" not in out
    assert world.fake.all_orders() == []


def test_no_trades_on_a_half_day(world: World) -> None:
    at_close(world)
    decided(world)
    world.set_session(TODAY, is_early_close=True)
    out = world.run(execute_close.make_job)
    assert out["skipped"] == "half day: no trades"
    assert world.fake.all_orders() == []


def test_incomplete_data_blocks_buys_but_not_the_cash_fund_already_held(world: World) -> None:
    at_close(world)
    Market(world.fake)
    fake = world.fake
    fake._book("BIL", Side.BUY, 100, 91.5)
    decided(world, {"SPY": 0.5, "BIL": 0.5})
    world.set_session(date(2026, 10, 21), data_status="INCOMPLETE")
    out = world.run(execute_close.make_job)
    assert any("data INCOMPLETE" in b for b in out["rebalance"]["blocks"])
    assert fake.position("SPY") == 0
    assert fake.position("BIL") > 100


def test_the_halt_holds_leverage_back_on_e(world: World) -> None:
    at_close(world)
    Market(world.fake)
    decided(world)
    world.set_flag("halt_drawdown", True)
    world.run(execute_close.make_job)
    # The leveraged fund's 30% goes to the market fund: 1.0 x 100,000 at 502.50.
    assert (world.fake.position("SPY"), world.fake.position("SSO")) == (199, 0)
    assert stop_levels(world) == []


def test_a_fund_without_a_price_is_owed_to_the_retry(world: World) -> None:
    at_close(world)
    Market(world.fake)
    world.fake.set_price("SSO", None)
    row = decided(world)
    out = world.run(execute_close.make_job)
    assert not out["rebalance"]["finished"]
    assert world.fake.position("SSO") == 0
    assert world.fake.position("SPY") == 139
    at_close(world, NEXT)
    world.fake.set_price("SSO", 100.0)
    world.run(execute_close.make_job, session=NEXT)
    assert world.fake.position("SSO") == 298
    assert target_row(world, row).executed_at is not None
    assert protected(world)


# --- stop_guard and the invariant for #155 -------------------------------------------------


def test_stop_guard_leaves_the_unleveraged_funds_alone(world: World) -> None:
    first_rebalance(world)
    out = world.run(stop_guard.make_job)
    assert out == {"ok": True}
    assert stops(world.fake, "SPY") == []


def test_stop_guard_replaces_a_deleted_stop_on_the_leveraged_fund_at_its_level(
    world: World,
) -> None:
    first_rebalance(world)
    fake = world.fake
    [leg] = stops(fake)
    fake.delete(leg.id)
    out = world.run(stop_guard.make_job)
    assert out["acted"]
    assert out["repaired"] == ["SSO"]
    [stop] = stops(fake)
    assert (stop.qty, stop.stop_price) == (298, 70.0)
    assert protected(world)


@pytest.mark.parametrize("mode", [TradingMode.PAPER, TradingMode.DRY_RUN])
def test_the_halts_sale_moves_the_leveraged_fund_into_the_market_fund(
    world: World, mode: TradingMode
) -> None:
    first_rebalance(world)
    fake = world.fake
    at_close(world, NEXT)
    world.set_flag("halt_drawdown", True)
    out = world.run(execute_close.make_job, session=NEXT, mode=mode)
    if mode is TradingMode.DRY_RUN:
        assert out["halt_sale"] == {"symbol": "SSO", "qty": 298, "level": 70.0, "dry_run": True}
        assert fake.position("SSO") == 298
        return
    assert fake.position("SSO") == 0
    assert stops(fake) == []
    # 298 x 100 of proceeds at the 502.50 limit.
    assert fake.position("SPY") == 139 + 59
    assert out["halt_sale"]["sale"]["status"] == "FILLED"
    assert no_duplicates(fake)
    assert protected(world)


# --- what else blocks buys, and the halt's sale when it can't go ---------------------------


def unknown_position(world: World) -> None:
    world.fake.set_price("NVDA", 99.0)
    world.fake._book("NVDA", Side.BUY, 1, 99.0)


def account_mismatch(world: World) -> None:
    with world.engine.begin() as conn:
        conn.execute(insert(Mismatch).values(kind="RECONCILIATION", symbol=None))


def deleted_stop(world: World) -> None:
    [leg] = stops(world.fake)
    world.fake.delete(leg.id)


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda w: w.set_session(NEXT, calendar_check="DISAGREE"), "calendar cross-check"),
        (account_mismatch, "an account-level mismatch is open"),
        (deleted_stop, "the stop invariant needed repair"),
        (unknown_position, "unknown positions at the broker"),
    ],
)
def test_other_reasons_block_buys(
    world: World, setup: Callable[[World], None], reason: str
) -> None:
    first_rebalance(world)
    at_close(world, NEXT)
    decided(world, {"SPY": 0.8, "SSO": 0.2}, execution=NEXT)
    setup(world)
    out = world.run(execute_close.make_job, session=NEXT)
    assert any(b.startswith(reason) for b in out["rebalance"]["blocks"])
    assert world.fake.position("SPY") == 139  # its buy held back
    assert world.fake.position("SSO") < 298  # the sale toward a lower target went ahead
    assert protected(world)


def test_no_rebalance_with_the_market_closed(world: World) -> None:
    at_close(world)
    world.fake.close_market()
    row = decided(world)
    out = world.run(execute_close.make_job)
    assert out["rebalance"]["skipped"] == "market closed"
    assert target_row(world, row).executed_at is None


def test_the_halts_sale_waits_for_a_price_and_for_the_market(world: World) -> None:
    first_rebalance(world)
    world.set_flag("halt_drawdown", True)
    at_close(world, NEXT)
    world.fake.set_price("SSO", None)
    out = world.run(execute_close.make_job, session=NEXT)
    assert out["halt_sale"]["skipped"] == "no price"
    world.fake.set_price("SSO", 100.0)
    world.fake.close_market()
    out = world.run(execute_close.make_job, session=NEXT)
    assert out["halt_sale"]["skipped"] == "market closed"
    assert world.fake.position("SSO") == 298
    assert protected(world)


def test_the_halts_sale_unfilled_is_owed_and_the_stop_restored(world: World) -> None:
    market = first_rebalance_market(world)
    world.set_flag("halt_drawdown", True)
    at_close(world, NEXT)
    market.off.add("SSO")
    out = world.run(execute_close.make_job, session=NEXT)
    assert out["halt_sale"]["sale"]["status"] == "UNFILLED"
    assert "buy" not in out["halt_sale"]
    assert any("the rest is owed" in t for t in world.alerts.texts)
    assert world.fake.position("SSO") == 298
    assert protected(world)


def test_the_halts_proceeds_stay_in_cash_without_a_market_price(world: World) -> None:
    first_rebalance(world)
    world.set_flag("halt_drawdown", True)
    at_close(world, NEXT)
    world.fake.set_price("SPY", None)
    out = world.run(execute_close.make_job, session=NEXT)
    assert out["halt_sale"]["sale"]["sold"] == 298
    assert "buy" not in out["halt_sale"]
    assert any("stay in cash" in t for t in world.alerts.texts)


def test_a_leveraged_fund_held_without_a_level_gets_one_where_a_buy_would_put_it(
    world: World,
) -> None:
    at_close(world)
    Market(world.fake)
    world.fake._book("SSO", Side.BUY, 50, 100.0)
    world.set_flag("halt_drawdown", True)
    out = world.run(execute_close.make_job)
    assert any("SSO: 50 shares with no plan" in t for t in world.alerts.texts)
    assert out["halt_sale"]["level"] == 70.0
    assert world.fake.position("SSO") == 0
