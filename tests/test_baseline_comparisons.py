"""The 3.8 pool comparisons: each variant changes what it says, and only that."""

from __future__ import annotations

from datetime import date

from auratrade.config import (
    DEFAULT_RISK_CONFIG,
    ExitPlan,
    ExitSetName,
    RiskConfig,
    Strategy,
    TrailMA,
    load_risk_config,
)
from auratrade.sim.trade import TradeOutcome, TradeResult
from backtest import comparisons as cmp
from backtest import pool as bp
from tests.test_baseline_backtest import found

RISK = load_risk_config(DEFAULT_RISK_CONFIG)


def plan(risk: RiskConfig, strategy: Strategy) -> ExitPlan:
    return risk.plans.default_exit_plan(strategy)


def test_every_variant_builds_and_changes_its_strategies() -> None:
    names = set()
    for v in cmp.variants(RISK):
        changed = v.change(RISK)
        names.add(v.name)
        assert changed is not RISK
        assert v.strategies
        assert changed.model_dump() != RISK.model_dump(), v.name
    assert {"one_tranche", "full_set", "floor", "partial_none", "trail_50_sma"} <= names


def test_exit_variants() -> None:
    floor = cmp.floor(RISK)
    for s in Strategy:
        p = plan(floor, s)
        assert p.trail is plan(RISK, s).trail
        assert (p.partial, p.round_trip, p.earnings_cushion) == (None, False, False)
    trail = cmp.trail(TrailMA.TEN_DAY)(RISK)
    assert {plan(trail, s).trail for s in Strategy} == {TrailMA.TEN_DAY}
    oneil = cmp.partial_of(ExitSetName.ONEIL)(RISK)
    assert (
        plan(oneil, Strategy.VCP_BREAKOUT).partial
        == RISK.plans.exit_sets[ExitSetName.ONEIL].partial
    )
    full = cmp.full_set(RISK)
    assert plan(full, Strategy.MOMENTUM_BREAKOUT).round_trip
    assert (
        plan(full, Strategy.MOMENTUM_BREAKOUT).trail is plan(RISK, Strategy.MOMENTUM_BREAKOUT).trail
    )


def test_entry_and_guardrail_variants() -> None:
    one = cmp.one_tranche(RISK)
    assert one.plans.entry[Strategy.VCP_BREAKOUT].tranches == 1
    assert one.plans.entry[Strategy.VCP_BREAKOUT].gate is None
    assert cmp.no_base_filter(RISK).guardrails.max_base_count == 99
    assert cmp.ep_stop_cap(1.0)(RISK).guardrails.stop_widths[Strategy.EPISODIC_PIVOT].max == 1.0
    up = cmp.under_pressure_all(RISK).regimes.UNDER_PRESSURE.strategies
    assert all(f.min_conviction is None for f in up if f.strategy is Strategy.VCP_BREAKOUT)


def closed(day: date, r: float) -> bp.PoolTrade:
    result = TradeResult(TradeOutcome.CLOSED, risk=100.0, pnl=100.0 * r)
    listing = bp.Listing(found("X", 0, 10.0), day, 1, 0.5, 4)
    return bp.PoolTrade(listing, day, bp.Regime.CORRECTION, bp.Status.CLOSED, result=result)


def test_the_difference_and_its_interval() -> None:
    days = [date(2020, 1, d) for d in range(6, 11)]
    ref = [closed(d, -1.0) for d in days] + [closed(days[0], 2.0)]
    var = [closed(d, 1.0) for d in days]
    d = cmp.difference(ref, var, None)
    assert (d.reference, d.variant, d.difference) == (-0.5, 1.0, 1.5)
    assert d.interval is not None
    assert d.beyond_noise
    assert not d.enough  # fewer than 30 trades a side
