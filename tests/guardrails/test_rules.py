"""The single-rule guardrails (README §8): levels, stop width, risk, heat, clusters, earnings,
blocks, tranche 2, tighten-only and exits."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from auratrade.config import Regime, Strategy
from auratrade.guardrails.blocks import EntryBlock, entries_blocked, loss_halts
from auratrade.guardrails.clusters import (
    ClusterMember,
    cluster_check,
    linked,
    return_correlation,
)
from auratrade.guardrails.earnings import earnings_blackout
from auratrade.guardrails.levels import StrategyRecord, extension, price_levels, stop_width
from auratrade.guardrails.model import GuardrailRule, Outcome, outcome_of
from auratrade.guardrails.positions import exit_check, tighten_check, tranche2_check
from auratrade.guardrails.risk import (
    OpenRisk,
    base_count_factor,
    effective_risk,
    exposure_check,
    fundable,
    heat_room,
    positions_check,
    regime_limits,
    risk_per_trade,
)
from auratrade.plans.entry import GateResult
from auratrade.plans.model import PlanChange
from auratrade.plans.versioning import Tightening
from tests.guardrails.factories import EQUITY, RISK, G, closes, holding

# --- Price levels --------------------------------------------------------------------------


def levels(**kw: float | None) -> GuardrailRule | None:
    args = {
        "pivot": 100.0,
        "stop": 94.0,
        "code_pivot": 100.0,
        "structural_stop": 93.0,
        "atr": 2.5,
    } | kw
    finding = price_levels(config=G, **args)  # type: ignore[arg-type]
    return None if finding is None else finding.rule


def test_levels_within_the_bounds_pass() -> None:
    assert levels() is None
    assert levels(pivot=103.0) is None  # +3% exactly
    assert levels(pivot=97.0) is None
    assert levels(stop=93.0) is None  # the structural stop itself


@pytest.mark.parametrize(
    ("kw", "rule"),
    [
        ({"stop": None}, GuardrailRule.STOP_REQUIRED),
        ({"pivot": 110.0}, GuardrailRule.PRICE_LEVELS),  # 10% off the code pivot
        ({"pivot": 96.9}, GuardrailRule.PRICE_LEVELS),
        ({"stop": 92.0}, GuardrailRule.PRICE_LEVELS),  # looser than the structural stop
        ({"stop": 99.0}, GuardrailRule.PRICE_LEVELS),  # 1 below the pivot < 0.5 x ATR
        ({"stop": 101.0}, GuardrailRule.PRICE_LEVELS),  # inverted
        ({"stop": -5.0}, GuardrailRule.INVALID_INPUT),
        ({"pivot": float("nan")}, GuardrailRule.INVALID_INPUT),
        ({"atr": None}, GuardrailRule.INVALID_INPUT),
        ({"atr": 0.0}, GuardrailRule.INVALID_INPUT),
    ],
)
def test_levels_out_of_bounds_are_rejected(
    kw: dict[str, float | None], rule: GuardrailRule
) -> None:
    assert levels(**kw) == rule


# --- Stop width and keep losses small -----------------------------------------------------


def width(
    strategy: Strategy = Strategy.VCP_BREAKOUT,
    record: StrategyRecord | None = None,
    **kw: float | None,
) -> GuardrailRule | None:
    args = {"trigger": 100.0, "limit": 102.0, "stop": 94.0, "atr": 2.5, "adr": 0.04} | kw
    finding = stop_width(strategy, record=record, config=G, **args)  # type: ignore[arg-type]
    return None if finding is None else finding.rule


def test_vcp_width_is_measured_from_the_limit() -> None:
    assert width() is None  # 8 below 102; max 8.16
    assert width(stop=93.0) is GuardrailRule.STOP_WIDTH  # 9 below 102


def test_vcp_chased_to_the_max_with_an_8pct_structural_stop_is_rejected() -> None:
    """8% below the pivot is about 12.4% below a limit at pivot x 1.05 (README §7.1)."""
    assert width(trigger=104.0, limit=105.0, stop=92.0) is GuardrailRule.STOP_WIDTH
    assert width(trigger=104.0, limit=105.0, stop=96.6) is None


def test_atr_and_adr_widths_are_measured_from_the_trigger() -> None:
    # Pullback: 1.5 x ATR 2.5 = 3.75 below the trigger 100.
    assert width(Strategy.LEADER_PULLBACK, stop=96.25) is None
    assert width(Strategy.LEADER_PULLBACK, stop=96.0) is GuardrailRule.STOP_WIDTH
    # Momentum: 1 x ADR 4% of 100 = 4; EP: 1.5 x 4 = 6.
    assert width(Strategy.MOMENTUM_BREAKOUT, stop=96.0) is None
    assert width(Strategy.MOMENTUM_BREAKOUT, stop=95.9) is GuardrailRule.STOP_WIDTH
    assert width(Strategy.EPISODIC_PIVOT, stop=94.0) is None
    assert width(Strategy.EPISODIC_PIVOT, stop=93.9) is GuardrailRule.STOP_WIDTH


@pytest.mark.parametrize(
    "kw",
    [{"atr": None}, {"adr": None}, {"stop": float("nan")}, {"limit": 0.0}, {"adr": -0.01}],
)
def test_width_with_invalid_input_is_rejected(kw: dict[str, float | None]) -> None:
    assert width(**kw) is GuardrailRule.INVALID_INPUT


def test_stop_at_or_above_the_trigger_is_rejected() -> None:
    assert width(stop=100.0) is GuardrailRule.STOP_WIDTH


def test_keep_losses_small_applies_after_20_trades() -> None:
    small_gains = StrategyRecord(closed_trades=19, average_gain=0.04)
    assert width(record=small_gains) is None  # too few trades: the default width
    # ½ x 10% = 5% of the 102 limit = 5.1: the 8-point stop is too wide, 5 fits.
    tens = StrategyRecord(closed_trades=20, average_gain=0.10)
    assert width(record=tens) is GuardrailRule.KEEP_LOSSES_SMALL
    assert width(record=tens, stop=97.0) is None
    # A large average gain leaves the default width in force.
    assert width(record=StrategyRecord(25, 0.40)) is None
    assert width(record=StrategyRecord(25, 0.40), stop=93.0) is GuardrailRule.STOP_WIDTH


def test_keep_losses_small_below_the_noise_floor_is_rejected() -> None:
    """½ x 4% of 102 = 2.04 < 1 x ATR 2.5: never squeeze the stop into normal noise."""
    record = StrategyRecord(closed_trades=20, average_gain=0.04)
    assert width(record=record, stop=99.0) is GuardrailRule.KEEP_LOSSES_SMALL
    # No winner in the window reads as a zero average gain: rejected.
    assert width(record=StrategyRecord(20, None), stop=99.0) is GuardrailRule.KEEP_LOSSES_SMALL


@given(
    adr=st.floats(0.005, 0.2),
    atr_share=st.floats(0.01, 1.0),
    trigger=st.floats(5, 1000),
    gain=st.floats(5.0, 50.0),
)
def test_momentum_and_ep_stop_at_1x_adr_pass_the_noise_floor(
    adr: float, atr_share: float, trigger: float, gain: float
) -> None:
    """Whenever ATR <= 2 x ADR, a stop 1 x ADR below the trigger passes the 0.5 x ATR noise
    floor (the regression test for the old 1 x ATR floor, which rejected them all)."""
    atr = 2 * adr * trigger * atr_share
    stop = trigger * (1 - adr) + 1e-9 * trigger
    record = StrategyRecord(closed_trades=40, average_gain=gain)  # the default width binds
    for strategy in (Strategy.MOMENTUM_BREAKOUT, Strategy.EPISODIC_PIVOT):
        assert (
            stop_width(
                strategy,
                trigger=trigger,
                limit=trigger * 1.02,
                stop=stop,
                atr=atr,
                adr=adr,
                record=record,
                config=G,
            )
            is None
        )


# --- The 50-day ---------------------------------------------------------------------------


def test_extension() -> None:
    assert extension(Strategy.VCP_BREAKOUT, price=125.0, sma_50=100.0, config=G) is None
    found = extension(Strategy.VCP_BREAKOUT, price=125.1, sma_50=100.0, config=G)
    assert found is not None
    assert found.rule is GuardrailRule.EXTENSION
    assert extension(Strategy.MOMENTUM_BREAKOUT, price=200.0, sma_50=100.0, config=G) is None
    assert extension(Strategy.EPISODIC_PIVOT, price=200.0, sma_50=None, config=G) is None
    missing = extension(Strategy.LEADER_PULLBACK, price=100.0, sma_50=None, config=G)
    assert missing is not None
    assert missing.rule is GuardrailRule.EXTENSION
    assert extension(Strategy.LEADER_PULLBACK, price=None, sma_50=100.0, config=G) is not None
    assert extension(Strategy.LEADER_PULLBACK, price=float("nan"), sma_50=1.0, config=G)


# --- Regime limits, fundability, effective risk ------------------------------------------


def test_regime_limits_follow_the_readme_table() -> None:
    cu = regime_limits(Regime.CONFIRMED_UPTREND, stepped=False, equity=EQUITY, config=RISK.regimes)
    assert (cu.base_risk, cu.max_exposure, cu.max_positions) == (0.01, 1.0, 6)
    big = regime_limits(
        Regime.CONFIRMED_UPTREND, stepped=False, equity=100_000, config=RISK.regimes
    )
    assert big.max_positions == 8
    up = regime_limits(Regime.UNDER_PRESSURE, stepped=False, equity=1e6, config=RISK.regimes)
    assert (up.base_risk, up.max_exposure, up.max_positions) == (0.005, 0.5, 4)
    corr = regime_limits(Regime.CORRECTION, stepped=True, equity=EQUITY, config=RISK.regimes)
    assert (corr.base_risk, corr.max_positions, corr.strategies) == (0.0, 0, ())


def test_early_uptrend_steps_up_only_when_stepped() -> None:
    starter = regime_limits(Regime.EARLY_UPTREND, stepped=False, equity=EQUITY, config=RISK.regimes)
    stepped = regime_limits(Regime.EARLY_UPTREND, stepped=True, equity=EQUITY, config=RISK.regimes)
    assert (starter.base_risk, starter.max_exposure) == (0.0025, 0.25)
    assert (stepped.base_risk, stepped.max_exposure) == (0.005, 0.5)


def test_fundable() -> None:
    up = regime_limits(Regime.UNDER_PRESSURE, stepped=False, equity=EQUITY, config=RISK.regimes)
    assert fundable(Strategy.VCP_BREAKOUT, 4, up, Regime.UNDER_PRESSURE) is None
    for conviction in (3, None):
        low = fundable(Strategy.VCP_BREAKOUT, conviction, up, Regime.UNDER_PRESSURE)
        assert low is not None
        assert low.rule is GuardrailRule.FUNDABLE
    assert fundable(Strategy.MOMENTUM_BREAKOUT, 5, up, Regime.UNDER_PRESSURE) is not None
    cu = regime_limits(Regime.CONFIRMED_UPTREND, stepped=False, equity=EQUITY, config=RISK.regimes)
    assert fundable(Strategy.MOMENTUM_BREAKOUT, None, cu, Regime.CONFIRMED_UPTREND) is None


@pytest.mark.parametrize(
    ("stage", "reliable", "factor"),
    [(None, False, 1.0), (1, True, 1.0), (2, True, 1.0), (3, True, 0.5), (1, False, 0.5)],
)
def test_base_count_factor(stage: int | None, reliable: bool, factor: float) -> None:
    assert base_count_factor(stage, reliable, G) == factor


def test_fourth_base_is_rejected() -> None:
    for reliable in (True, False):
        found = base_count_factor(4, reliable, G)
        assert not isinstance(found, float)
        assert found.rule is GuardrailRule.BASE_COUNT


def test_effective_risk_is_capped_at_1_5pct() -> None:
    assert effective_risk(0.01, 1.25, 1.0, G) == pytest.approx(0.0125)
    assert effective_risk(0.01, 1.25, 0.5, G) == pytest.approx(0.00625)
    assert effective_risk(0.02, 1.25, 1.0, G) == 0.015


def test_requested_risk_is_clipped_to_the_effective_risk() -> None:
    assert risk_per_trade(None, 0.01) == (0.01, None)
    assert risk_per_trade(0.005, 0.01) == (0.005, None)
    value, finding = risk_per_trade(0.5, 0.01)  # an LLM asking for 50%
    assert value == 0.01
    assert finding is not None
    assert (finding.rule, finding.outcome) == (GuardrailRule.MAX_RISK, Outcome.CLIP)


# --- Heat ---------------------------------------------------------------------------------


def test_open_risk_ignores_stops_at_breakeven_or_higher() -> None:
    assert OpenRisk(10, 100.0, 92.0, 110.0).risk == 80.0
    assert OpenRisk(10, 100.0, 101.0, 110.0).risk == 0.0
    assert OpenRisk(10, 100.0, 92.0, 110.0).exposure == 1100.0


def test_heat_clip_regression() -> None:
    """4 positions at 1.25% open: a 5th at 1.25% is clipped to the 1% left, not rejected."""
    amount, finding = heat_room(125.0, open_heat=500.0, equity=EQUITY, config=G)
    assert amount == pytest.approx(100.0)
    assert finding is not None
    assert (finding.rule, finding.outcome) == (GuardrailRule.HEAT, Outcome.CLIP)


def test_heat_with_room() -> None:
    assert heat_room(100.0, open_heat=500.0, equity=EQUITY, config=G) == (100.0, None)


@pytest.mark.parametrize("open_heat", [600.0, 700.0, 551.0])
def test_heat_at_the_cap_or_under_half_a_trade_is_rejected(open_heat: float) -> None:
    amount, finding = heat_room(100.0, open_heat=open_heat, equity=EQUITY, config=G)
    assert amount == 0.0
    assert finding is not None
    assert (finding.rule, finding.outcome) == (GuardrailRule.HEAT, Outcome.REJECT)


def test_positions_and_exposure() -> None:
    cu = regime_limits(Regime.CONFIRMED_UPTREND, stepped=False, equity=EQUITY, config=RISK.regimes)
    assert positions_check(5, cu, Regime.CONFIRMED_UPTREND) is None
    full = positions_check(6, cu, Regime.CONFIRMED_UPTREND)
    assert full is not None
    assert full.rule is GuardrailRule.MAX_POSITIONS
    assert exposure_check(1000.0, open_exposure=9000.0, equity=EQUITY, limits=cu) is None
    margin = exposure_check(1000.1, open_exposure=9000.0, equity=EQUITY, limits=cu)
    assert margin is not None
    assert margin.rule is GuardrailRule.NO_MARGIN
    up = regime_limits(Regime.UNDER_PRESSURE, stepped=False, equity=EQUITY, config=RISK.regimes)
    over = exposure_check(1000.0, open_exposure=4500.0, equity=EQUITY, limits=up)
    assert over is not None
    assert over.rule is GuardrailRule.MAX_EXPOSURE


# --- Clusters -----------------------------------------------------------------------------


def test_return_correlation() -> None:
    a = closes(1)
    assert return_correlation(a, a, 60) == pytest.approx(1.0)
    assert return_correlation(a, [2 * c for c in a], 60) == pytest.approx(1.0)
    independent = return_correlation(a, closes(2), 60)
    assert independent is not None
    assert abs(independent) < 0.7
    assert return_correlation(a[:60], a, 60) is None  # 59 returns
    assert return_correlation(a, [100.0] * 80, 60) is None  # flat
    assert return_correlation(a, [*a[:-1], float("nan")], 60) is None


def member(symbol: str, seed: int, industry: str | None = None) -> ClusterMember:
    return ClusterMember(symbol, industry, closes(seed))


def test_linked_by_industry_or_correlation_or_missing_data() -> None:
    assert linked(member("A", 1, "SEMIS"), member("B", 2, "SEMIS"), G)
    assert not linked(member("A", 1, "SEMIS"), member("B", 2, "SOFTWARE"), G)
    assert not linked(member("A", 1), member("B", 2), G)
    twin = ClusterMember("B", "OTHER", [c * 1.5 for c in closes(1)])
    assert linked(member("A", 1, "SEMIS"), twin, G)
    assert linked(member("A", 1), ClusterMember("B", None, []), G)  # can't compute: fail closed


def test_third_member_of_a_cluster_is_rejected() -> None:
    candidate = member("NEW", 9, "SEMIS")
    one = [member("A", 1, "SEMIS"), member("B", 2, "SOFTWARE")]
    assert cluster_check(candidate, one, G) is None
    two = [member("A", 1, "SEMIS"), member("B", 2, "SEMIS")]
    found = cluster_check(candidate, two, G)
    assert found is not None
    assert found.rule is GuardrailRule.CLUSTER


# --- Earnings blackout --------------------------------------------------------------------


def blackout(
    strategy: Strategy = Strategy.VCP_BREAKOUT, nxt: int | None = None, last: int | None = None
) -> GuardrailRule | None:
    found = earnings_blackout(strategy, next_report_in=nxt, last_report_ago=last, config=G)
    return None if found is None else found.rule


def test_known_next_report() -> None:
    assert blackout(nxt=6) is None
    assert blackout(nxt=5) is GuardrailRule.EARNINGS_BLACKOUT
    assert blackout(nxt=0) is GuardrailRule.EARNINGS_BLACKOUT
    assert blackout(nxt=-1) is GuardrailRule.INVALID_INPUT


def test_unknown_next_report_is_estimated() -> None:
    """Estimated at last + 63 sessions, blacked out 10 either side."""
    assert blackout(last=52) is None
    assert blackout(last=53) is GuardrailRule.EARNINGS_BLACKOUT
    assert blackout(last=73) is GuardrailRule.EARNINGS_BLACKOUT
    assert blackout(last=74) is None
    assert blackout() is GuardrailRule.EARNINGS_BLACKOUT  # no report history
    assert blackout(last=-1) is GuardrailRule.EARNINGS_BLACKOUT


def test_eps_are_exempt_from_the_blackout() -> None:
    assert blackout(Strategy.EPISODIC_PIVOT, nxt=0) is None
    assert blackout(Strategy.EPISODIC_PIVOT) is None


# --- Blocks -------------------------------------------------------------------------------


def test_loss_halts() -> None:
    halts = RISK.halts
    assert loss_halts(day_return=-0.019, week_return=-0.04, drawdown=0.1, config=halts) == set()
    assert loss_halts(day_return=-0.02, week_return=-0.05, drawdown=0.15, config=halts) == {
        EntryBlock.DAILY_LOSS_HALT,
        EntryBlock.WEEKLY_LOSS_HALT,
        EntryBlock.DRAWDOWN_HALT,
    }
    unknown = loss_halts(day_return=float("nan"), week_return=0.0, drawdown=0.0, config=halts)
    assert len(unknown) == 3


def test_entries_blocked() -> None:
    assert entries_blocked(frozenset()) is None
    found = entries_blocked({EntryBlock.CRISIS_OVERRIDE, EntryBlock.KILL_SWITCH})
    assert found is not None
    assert found.rule is GuardrailRule.ENTRIES_BLOCKED
    assert "CRISIS_OVERRIDE, KILL_SWITCH" in found.message


# --- Tranche 2, tightenings, exits --------------------------------------------------------

PASSED = GateResult(())


def t2(
    gate: GateResult = PASSED, session: int = 2, blocked: bool = False, **kw: object
) -> str | None:
    h = holding("HELD", seed=1, **kw)  # type: ignore[arg-type]
    found = tranche2_check(h.plan, gate=gate, session=session, blocked_by_plan=blocked, config=G)
    return None if found is None else found.message


def test_tranche2_rules() -> None:
    assert t2() is None
    assert t2(session=1) is None
    assert t2(session=3) is None
    assert "window" in (t2(session=4) or "")
    assert "window" in (t2(session=0) or "")
    assert "gate failed: weak volume" in (t2(GateResult(("weak volume",))) or "")
    assert "blocked" in (t2(blocked=True) or "")
    assert "no tranche 2" in (t2(strategy=Strategy.MOMENTUM_BREAKOUT) or "")
    filled = holding("HELD", seed=1)
    plan = filled.plan.evolve(tranche2_filled=True)
    found = tranche2_check(plan, gate=PASSED, session=2, blocked_by_plan=False, config=G)
    assert found is not None


def test_tighten_only() -> None:
    plan = holding("HELD", seed=1, stop=92.0).plan
    raised = tighten_check(plan, Tightening(hard_stop=95.0), price=101.0, config=RISK.plans)
    assert not isinstance(raised, type(None))
    assert getattr(raised, "change", None) is PlanChange.TIGHTEN
    below_initial = tighten_check(plan, Tightening(hard_stop=90.0), price=101.0, config=RISK.plans)
    assert getattr(below_initial, "rule", None) is GuardrailRule.TIGHTEN_ONLY
    assert isinstance(raised, type(plan))
    lowered = tighten_check(raised, Tightening(hard_stop=93.0), price=101.0, config=RISK.plans)
    assert getattr(lowered, "rule", None) is GuardrailRule.TIGHTEN_ONLY
    no_price = tighten_check(
        plan, Tightening(hard_stop=95.0), price=float("nan"), config=RISK.plans
    )
    assert getattr(no_price, "rule", None) is GuardrailRule.INVALID_INPUT


def test_exits() -> None:
    assert exit_check(None, 10) is None
    assert exit_check(10, 10) is None
    assert exit_check(11, 10) is not None
    assert exit_check(0, 10) is not None


def test_outcome_of() -> None:
    assert outcome_of([]) is Outcome.APPROVE
