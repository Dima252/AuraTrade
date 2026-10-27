"""The rebalance's orders, pure (`execution.rebalance`, task 5.1): hand-built holdings with
known answers."""

from __future__ import annotations

from datetime import date

import pytest

from auratrade.config import PortfolioConfig, load_risk_config
from auratrade.execution import ids
from auratrade.execution.broker import BrokerOrder, OrderClass, OrderStatus, OrderType, Side
from auratrade.execution.broker import TimeInForce as Tif
from auratrade.execution.prices import marketable_buy_limit
from auratrade.execution.rebalance import (
    Action,
    Blocks,
    Move,
    Why,
    allow_buys,
    buy_block,
    fit_buys,
    halted_targets,
    leveraged_stop,
    plan,
)
from auratrade.execution.store import order_purpose

CFG: PortfolioConfig = load_risk_config().portfolio
SLIP = 0.005
UP = {"SPY": 0.7, "SSO": 0.3}
PRICES: dict[str, float | None] = {"SPY": 500.0, "SSO": 100.0, "BIL": 91.5, "QQQ": 400.0}


def by_symbol(moves: list[Move]) -> dict[str, Move]:
    return {m.symbol: m for m in moves}


def test_the_first_rebalance_buys_every_target_from_cash_sized_on_the_limit() -> None:
    moves = by_symbol(plan(UP, None, {}, PRICES, 100_000.0, CFG, SLIP))
    spy, sso = moves["SPY"], moves["SSO"]
    assert (spy.action, spy.why, spy.limit) == (Action.BUY, Why.FIRST, 502.5)
    assert spy.qty == 139  # 70,000 / 502.50 = 139.3
    assert (sso.action, sso.qty, sso.limit) == (Action.BUY, 298, 100.5)  # 30,000 / 100.50
    assert not spy.owed
    assert not sso.owed


def test_sells_come_first_then_buys_then_holds() -> None:
    held = {"QQQ": 50, "SPY": 140, "BIL": 10}
    targets = {"SPY": 0.7, "SSO": 0.3}
    moves = plan(targets, {"SPY": 0.7, "QQQ": 0.2, "BIL": 0.1}, held, PRICES, 100_000.0, CFG, SLIP)
    assert [(m.symbol, m.action) for m in moves] == [
        ("BIL", Action.SELL),
        ("QQQ", Action.SELL),
        ("SSO", Action.BUY),
        ("SPY", Action.HOLD),
    ]
    assert by_symbol(moves)["QQQ"].qty == 50  # a target of 0 sells everything
    assert by_symbol(moves)["SPY"].why is Why.WITHIN_BAND


def test_a_holding_within_the_band_with_an_unchanged_target_isnt_traded() -> None:
    # 66% held against 70%: 4 points off, inside the 5-point band.
    moves = by_symbol(plan(UP, UP, {"SPY": 132, "SSO": 300}, PRICES, 100_000.0, CFG, SLIP))
    assert moves["SPY"].action is Action.HOLD
    assert moves["SPY"].why is Why.WITHIN_BAND
    assert moves["SPY"].weight == pytest.approx(0.66)


def test_a_holding_past_the_band_goes_back_to_its_target() -> None:
    # 76% held against 70%: sold to floor(70,000 / 500) = 140 shares.
    moves = by_symbol(plan(UP, UP, {"SPY": 152, "SSO": 240}, PRICES, 100_000.0, CFG, SLIP))
    assert (moves["SPY"].action, moves["SPY"].why, moves["SPY"].qty) == (
        Action.SELL,
        Why.DRIFTED,
        12,
    )
    assert moves["SSO"].why is Why.DRIFTED  # 24% against 30%
    assert (moves["SSO"].action, moves["SSO"].qty) == (Action.BUY, 298 - 240)


def test_a_changed_target_is_traded_even_inside_the_band() -> None:
    previous = {"SPY": 0.69, "SSO": 0.31}
    moves = by_symbol(plan(UP, previous, {"SPY": 138, "SSO": 310}, PRICES, 100_000.0, CFG, SLIP))
    assert moves["SPY"].why is Why.TARGET_CHANGED
    assert (moves["SPY"].action, moves["SPY"].qty) == (Action.BUY, 1)  # 139 at the limit
    assert (moves["SSO"].action, moves["SSO"].qty) == (Action.SELL, 10)


def test_whole_shares_already_at_the_target_are_left_alone() -> None:
    # A hair above the target value (float noise): every share is kept, no sale of one.
    above = by_symbol(plan({"SPY": 1.0}, None, {"SPY": 200}, PRICES, 100_000 - 1e-8, CFG, SLIP))
    assert (above["SPY"].action, above["SPY"].why) == (Action.HOLD, Why.AT_TARGET)
    # $100 above it: one share is sold.
    sold = by_symbol(plan({"SPY": 1.0}, None, {"SPY": 200}, PRICES, 99_900.0, CFG, SLIP))
    assert (sold["SPY"].action, sold["SPY"].qty) == (Action.SELL, 1)
    # Slightly below it, but the limit's slippage leaves no whole share to buy.
    below = by_symbol(plan({"SPY": 1.0}, None, {"SPY": 199}, PRICES, 100_000.0, CFG, SLIP))
    assert (below["SPY"].action, below["SPY"].why) == (Action.HOLD, Why.AT_TARGET)


def test_a_fund_without_a_price_is_owed_only_if_it_would_be_traded() -> None:
    prices: dict[str, float | None] = {**PRICES, "SSO": None}
    unchanged = by_symbol(plan(UP, UP, {"SPY": 140, "SSO": 300}, prices, 100_000.0, CFG, SLIP))
    assert (unchanged["SSO"].why, unchanged["SSO"].owed) == (Why.NO_PRICE, False)
    assert unchanged["SSO"].weight is None
    first = by_symbol(plan(UP, None, {}, prices, 100_000.0, CFG, SLIP))
    assert (first["SSO"].action, first["SSO"].why, first["SSO"].owed) == (
        Action.HOLD,
        Why.NO_PRICE,
        True,
    )


def test_a_negative_target_counts_as_none() -> None:
    moves = by_symbol(plan({"SPY": -0.1}, None, {"SPY": 10}, PRICES, 100_000.0, CFG, SLIP))
    assert (moves["SPY"].action, moves["SPY"].qty) == (Action.SELL, 10)


def test_equity_must_be_positive() -> None:
    with pytest.raises(ValueError, match="equity"):
        plan(UP, None, {}, PRICES, 0.0, CFG, SLIP)


# --- what may be bought -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("symbol", "held", "blocks", "reason"),
    [
        ("SPY", 0, Blocks(), None),
        ("SSO", 0, Blocks(halted=True), "the drawdown halt: no leverage"),
        ("SPY", 0, Blocks(halted=True), None),
        ("QQQ", 5, Blocks(mismatched=frozenset({"QQQ"})), "an open mismatch in the fund"),
        ("SPY", 10, Blocks(increases=("kill switch on",)), "increases blocked: kill switch on"),
        ("BIL", 10, Blocks(increases=("kill switch on",)), None),
        (
            "BIL",
            0,
            Blocks(increases=("kill switch on", "x")),
            "increases blocked: kill switch on; x",
        ),
    ],
)
def test_buy_block(symbol: str, held: int, blocks: Blocks, reason: str | None) -> None:
    assert buy_block(symbol, held, blocks, CFG) == reason


def test_blocked_buys_become_owed_holds_and_sales_go_ahead() -> None:
    moves = plan(UP, None, {"QQQ": 10}, PRICES, 100_000.0, CFG, SLIP)
    allowed = by_symbol(allow_buys(moves, Blocks(increases=("kill switch on",)), CFG))
    assert allowed["QQQ"].action is Action.SELL
    for symbol in ("SPY", "SSO"):
        assert (allowed[symbol].action, allowed[symbol].why, allowed[symbol].qty) == (
            Action.HOLD,
            Why.BLOCKED,
            0,
        )
        assert allowed[symbol].owed
        assert allowed[symbol].note == "increases blocked: kill switch on"


def test_buys_are_scaled_into_the_cash_the_sales_left() -> None:
    moves = plan(UP, None, {}, PRICES, 100_000.0, CFG, SLIP)
    assert fit_buys(moves, 100_000.0) == moves
    half = by_symbol(fit_buys(moves, 50_000.0))
    assert (half["SPY"].qty, half["SSO"].qty) == (69, 149)
    assert half["SPY"].qty * 502.5 + half["SSO"].qty * 100.5 <= 50_000.0
    assert half["SPY"].note == "139 wanted, 69 fit the cash"


def test_a_buy_the_cash_cant_pay_for_isnt_owed() -> None:
    moves = plan(UP, None, {"QQQ": 1}, PRICES, 100_000.0, CFG, SLIP)
    fitted = by_symbol(fit_buys(moves, -5.0))
    assert fitted["QQQ"].action is Action.SELL
    assert (fitted["SPY"].action, fitted["SPY"].why, fitted["SPY"].owed) == (
        Action.HOLD,
        Why.NO_CASH,
        False,
    )


# --- the halt and the stop ----------------------------------------------------------------


def test_while_halted_the_leveraged_weight_goes_to_the_market_fund() -> None:
    assert halted_targets({"SPY": 0.5, "SSO": 0.3, "QQQ": 0.2}, CFG) == {
        "SPY": pytest.approx(0.8),
        "QQQ": 0.2,
    }
    assert halted_targets({"QQQ": 0.2, "SSO": 0.0}, CFG) == {"QQQ": 0.2}


def test_the_stop_is_set_from_flat_and_only_raised_after() -> None:
    assert leveraged_stop(100.0, None, CFG) == 70.0
    assert leveraged_stop(100.017, None, CFG) == 70.01  # rounded down to the tick
    assert leveraged_stop(90.0, 70.0, CFG) == 70.0  # never lowered
    assert leveraged_stop(120.0, 70.0, CFG) == 84.0


def test_marketable_buy_limit_rounds_up_to_the_tick() -> None:
    assert marketable_buy_limit(100.0, SLIP) == 100.5
    assert marketable_buy_limit(91.53, SLIP) == 91.99  # 91.98765 up


# --- ids ------------------------------------------------------------------------------------


def test_a_buy_id_parses_and_mirrors_as_an_entry() -> None:
    session = date(2026, 11, 2)
    cid = ids.buy_id(session, "SSO")
    parsed = ids.parse(cid)
    assert parsed is not None
    assert (parsed.session, parsed.symbol, parsed.purpose, parsed.detail) == (
        session,
        "SSO",
        ids.Purpose.BUY,
        "",
    )
    order = BrokerOrder(
        id="1",
        client_order_id=cid,
        symbol="SSO",
        side=Side.BUY,
        type=OrderType.LIMIT,
        time_in_force=Tif.GTC,
        order_class=OrderClass.OTO,
        qty=1,
        filled_qty=0,
        status=OrderStatus.NEW,
    )
    assert order_purpose(order) == "ENTRY"
