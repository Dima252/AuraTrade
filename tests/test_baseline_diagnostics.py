"""The 3.8 diagnostics: buckets, per-trade facts, the zero-cost config and the split rule."""

from __future__ import annotations

from datetime import date

from auratrade.config import DEFAULT_RISK_CONFIG, Strategy, load_risk_config
from auratrade.execution.sizing import Tranches
from auratrade.execution.triggers import OrderKind
from auratrade.guardrails.orders import OrderPlan
from auratrade.sim.trade import FillKind, TradeFill, TradeOutcome, TradeResult
from backtest import diagnostics as dg
from backtest import pool as bp
from tests.test_baseline_backtest import found

RISK = load_risk_config(DEFAULT_RISK_CONFIG)


def trade(day: date, r: float, *, fills: tuple[TradeFill, ...] = ()) -> bp.PoolTrade:
    order = OrderPlan(OrderKind.STOP_LIMIT, 10.0, 10.2, 9.5, Tranches(10, 0))
    result = TradeResult(TradeOutcome.CLOSED, order=order, fills=fills, risk=100.0, pnl=100.0 * r)
    listing = bp.Listing(found("X", 0, 10.0), day, 1, 0.5, 4)
    return bp.PoolTrade(listing, day, bp.Regime.CORRECTION, bp.Status.CLOSED, result=result)


def test_bucket_edges() -> None:
    labels = ("low", "mid", "high")
    assert dg.bucket(None, (1, 2), labels) is None
    assert dg.bucket(0.5, (1, 2), labels) == "low"
    assert dg.bucket(1, (1, 2), labels) == "mid"
    assert dg.bucket(5, (1, 2), labels) == "high"


def test_zero_cost_zeroes_every_tier_and_nothing_else() -> None:
    free = dg.zero_cost(RISK)
    assert [t.fraction for t in free.sim.slippage] == [0.0] * len(RISK.sim.slippage)
    assert [t.min_dollar_volume for t in free.sim.slippage] == [
        t.min_dollar_volume for t in RISK.sim.slippage
    ]
    assert free.guardrails == RISK.guardrails
    assert RISK.sim.slippage[0].fraction > 0


def test_stop_width_and_exit_session() -> None:
    fills = (
        TradeFill(1, FillKind.ENTRY, 10, 10.0),
        TradeFill(4, FillKind.EXIT, 10, 9.5),
    )
    t = trade(date(2020, 1, 6), -1.0, fills=fills)
    assert dg.entry_fill(t) == 10.0
    assert dg.stop_width(t) == 0.05
    assert dg.exit_session(t) == 4
    assert dg.chase(t) is None  # no approved entry, so no pivot
    bare = trade(date(2020, 1, 6), 0.0)
    assert (dg.entry_fill(bare), dg.stop_width(bare), dg.exit_session(bare)) == (None, None, None)


def year_days(year: int, n: int) -> list[date]:
    return [date(year, 1 + k // 20, 1 + k % 20) for k in range(n)]


def test_a_split_is_informative_only_with_a_clear_and_steady_gap() -> None:
    good = [trade(d, 1.0) for y in range(2017, 2023) for d in year_days(y, 12)]
    bad = [trade(d, -1.0) for y in range(2017, 2023) for d in year_days(y, 12)]
    reading = dg.read_split({"good": good, "bad": bad})
    assert reading is not None
    assert (reading.best, reading.worst) == ("good", "bad")
    assert (reading.years_won, reading.years_counted) == (6, 6)
    assert reading.informative
    # The same gap in only two years doesn't count.
    short = dg.read_split(
        {
            "good": [trade(d, 1.0) for y in (2017, 2018) for d in year_days(y, 20)],
            "bad": [trade(d, -1.0) for y in (2017, 2018) for d in year_days(y, 20)],
        }
    )
    assert short is not None
    assert not short.informative
    # Buckets under 30 trades aren't read.
    assert dg.read_split({"good": good, "bad": bad[:5]}) is None


def test_group_drops_trades_without_a_bucket() -> None:
    ts = [trade(date(2020, 1, 6), 1.0), trade(date(2020, 1, 7), -1.0)]
    g = dg.group(ts, lambda t: "win" if (t.r or 0) > 0 else None)
    assert list(g) == ["win"]
    assert len(g["win"]) == 1
    assert dg.mean_r(ts) == 0.0
    assert dg.closed(ts) == ts
    assert Strategy.VCP_BREAKOUT in dg.CHARTED
