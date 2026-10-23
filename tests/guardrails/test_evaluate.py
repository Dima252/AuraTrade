"""``evaluate(actions, context)`` end to end (README §8)."""

from dataclasses import replace

import pytest

from auratrade.config import ExitPlan, Regime, Strategy, TrailMA
from auratrade.guardrails.blocks import EntryBlock
from auratrade.guardrails.evaluate import (
    ApprovedEntry,
    Decision,
    Entry,
    Exit,
    RegimeView,
    Tighten,
    Tranche2,
    evaluate,
)
from auratrade.guardrails.levels import StrategyRecord
from auratrade.guardrails.model import GuardrailRule, Outcome
from auratrade.plans.entry import GateResult
from auratrade.plans.model import PositionPlan
from auratrade.plans.versioning import Tightening
from tests.guardrails.factories import EQUITY, RISK, context, entry, holding, pending, setup


def one(action: object, ctx: object = None) -> Decision:
    result = evaluate([action], ctx or context(), RISK)  # type: ignore[list-item, arg-type]
    return result.decisions[0]


def rules(decision: Decision) -> list[GuardrailRule]:
    return [f.rule for f in decision.findings]


def approved(decision: Decision) -> ApprovedEntry:
    assert isinstance(decision.approved, ApprovedEntry), decision.findings
    return decision.approved


# --- Approval -----------------------------------------------------------------------------


def test_a_clean_entry_is_approved() -> None:
    decision = one(entry())
    assert decision.outcome is Outcome.APPROVE
    a = approved(decision)
    assert (a.risk, a.risk_amount, a.planned_limit) == (0.01, 100.0, 102.0)
    assert (a.planned_shares, a.tranches, a.reserved_exposure) == (12, 2, 1224.0)
    assert a.exits == RISK.plans.default_exit_plan(Strategy.VCP_BREAKOUT)


def test_the_pm_exit_plan_is_kept() -> None:
    exits = RISK.plans.default_exit_plan(Strategy.VCP_BREAKOUT).model_copy(
        update={"trail": TrailMA.EMA_21}
    )
    assert approved(one(entry(exits=exits))).exits.trail is TrailMA.EMA_21


def test_an_exit_plan_outside_the_menu_is_rejected() -> None:
    exits = ExitPlan.model_validate(
        {
            **RISK.plans.default_exit_plan(Strategy.VCP_BREAKOUT).model_dump(),
            "partial": {"kind": "MINERVINI", "fraction": 0.9, "r_multiple": 2.0},
        }
    )
    assert rules(one(entry(exits=exits))) == [GuardrailRule.EXIT_PLAN]


def test_requested_risk_is_clipped_and_reported() -> None:
    decision = one(entry(risk=0.5))
    assert decision.outcome is Outcome.CLIP
    assert rules(decision) == [GuardrailRule.MAX_RISK]
    assert approved(decision).risk == 0.01


def test_lower_requested_risk_is_kept() -> None:
    assert approved(one(entry(risk=0.005))).risk_amount == 50.0


def test_multiplier_and_late_stage_scale_the_risk() -> None:
    ctx = context(setup(base_stage=3), multiplier=1.25)
    assert approved(one(entry(), ctx)).risk == pytest.approx(0.00625)


def test_unreliable_base_count_is_half_risk() -> None:
    ctx = context(setup(base_stage=2, base_reliable=False))
    assert approved(one(entry(), ctx)).risk == pytest.approx(0.005)


def test_position_size_clip() -> None:
    """A tight stop on 1% risk would buy $3,400; clipped to 25% of equity."""
    decision = one(entry(stop=99.0), context(setup(atr=1.0, structural_stop=98.0)))
    assert GuardrailRule.POSITION_SIZE in rules(decision)
    a = approved(decision)
    assert a.planned_shares == 24  # 2500 / 102
    assert a.reserved_exposure <= 0.25 * EQUITY


def test_small_size_goes_in_as_one_tranche() -> None:
    """At $5,000 a pricey leader sizes to one share (#122)."""
    ctx = context(setup(pivot=1000.0, structural_stop=930.0, atr=25.0, price=990.0, sma_50=900.0))
    # $50 at risk / (1020 - 975) = 1.1 shares.
    decision = one(entry(pivot=1000.0, stop=975.0), replace(ctx, equity=5000.0))
    a = approved(decision)
    assert (a.planned_shares, a.tranches) == (1, 1)
    assert GuardrailRule.WHOLE_SHARES in rules(decision)


def test_zero_shares_is_rejected() -> None:
    ctx = context(setup(pivot=1000.0, structural_stop=930.0, atr=25.0, price=990.0, sma_50=900.0))
    decision = one(entry(pivot=1000.0, stop=940.0), replace(ctx, equity=2000.0))
    assert rules(decision) == [GuardrailRule.WHOLE_SHARES]


# --- The regime ---------------------------------------------------------------------------


def test_regime_ceiling() -> None:
    ctx = context(regime=Regime.UNDER_PRESSURE)
    result = evaluate([RegimeView(Regime.CONFIRMED_UPTREND), entry()], ctx, RISK)
    assert result.regime is Regime.UNDER_PRESSURE
    assert rules(result.decisions[0]) == [GuardrailRule.REGIME_CEILING]
    # VCP at conviction 3 isn't fundable under pressure.
    assert rules(result.decisions[1]) == [GuardrailRule.FUNDABLE]


def test_a_lower_view_is_applied_without_a_finding() -> None:
    result = evaluate([entry(), RegimeView(Regime.CORRECTION)], context(), RISK)
    assert result.regime is Regime.CORRECTION
    assert result.decisions[1].findings == ()
    assert rules(result.decisions[0]) == [GuardrailRule.FUNDABLE]
    assert evaluate([RegimeView(None)], context(), RISK).decisions[0].findings == ()


def test_the_llm_cannot_assign_early_uptrend() -> None:
    result = evaluate([RegimeView(Regime.EARLY_UPTREND)], context(), RISK)
    assert result.regime is Regime.CORRECTION
    assert rules(result.decisions[0]) == [GuardrailRule.REGIME_CEILING]


def test_early_uptrend_step_up() -> None:
    el = setup(strategy=Strategy.EMERGING_LEADER)
    action = entry(strategy=Strategy.EMERGING_LEADER)
    starter = context(el, regime=Regime.EARLY_UPTREND)
    stepped = replace(starter, early_uptrend_stepped=True)
    assert approved(one(action, starter)).risk == 0.0025
    assert approved(one(action, stepped)).risk == 0.005


def test_correction_funds_nothing() -> None:
    assert rules(one(entry(), context(regime=Regime.CORRECTION))) == [GuardrailRule.FUNDABLE]


# --- Rejections, in pipeline order --------------------------------------------------------


@pytest.mark.parametrize(
    ("ctx_kw", "setup_kw", "entry_kw", "rule"),
    [
        ({"equity": float("nan")}, {}, {}, GuardrailRule.INVALID_INPUT),
        ({"multiplier": 0.0}, {}, {}, GuardrailRule.INVALID_INPUT),
        ({"blocks": frozenset({EntryBlock.KILL_SWITCH})}, {}, {}, GuardrailRule.ENTRIES_BLOCKED),
        ({}, {}, {"symbol": "ZZZ"}, GuardrailRule.TRADABLE_SYMBOL),
        ({}, {}, {"strategy": Strategy.LEADER_PULLBACK}, GuardrailRule.TRADABLE_SYMBOL),
        ({}, {}, {"stop": None}, GuardrailRule.STOP_REQUIRED),
        ({}, {}, {"pivot": 110.0}, GuardrailRule.PRICE_LEVELS),
        ({}, {"adr": None}, {}, GuardrailRule.INVALID_INPUT),
        ({}, {"price": 0.0}, {}, GuardrailRule.INVALID_INPUT),
        ({}, {"structural_stop": 90.0}, {"stop": 92.0}, GuardrailRule.STOP_WIDTH),
        ({}, {"price": 120.0, "sma_50": 90.0}, {}, GuardrailRule.EXTENSION),
        ({}, {"base_stage": 4}, {}, GuardrailRule.BASE_COUNT),
        ({}, {"next_report_in": 3}, {}, GuardrailRule.EARNINGS_BLACKOUT),
        ({}, {}, {"risk": -0.01}, GuardrailRule.INVALID_INPUT),
        ({}, {}, {"risk": float("nan")}, GuardrailRule.INVALID_INPUT),
    ],
)
def test_rejections(
    ctx_kw: dict[str, object],
    setup_kw: dict[str, object],
    entry_kw: dict[str, object],
    rule: GuardrailRule,
) -> None:
    decision = one(entry(**entry_kw), context(setup(**setup_kw), **ctx_kw))
    assert decision.outcome is Outcome.REJECT
    assert rules(decision)[-1] is rule
    assert decision.approved is None


def test_keep_losses_small_uses_the_strategy_record() -> None:
    ctx = context(records={Strategy.VCP_BREAKOUT: StrategyRecord(20, 0.10)})
    assert rules(one(entry(), ctx)) == [GuardrailRule.KEEP_LOSSES_SMALL]


def test_momentum_stop_width_waits_for_0936() -> None:
    """A low-of-day stop is set at 09:36; the proposal's invalidation level isn't its width."""
    mb = setup(strategy=Strategy.MOMENTUM_BREAKOUT, structural_stop=85.0, adr=0.04)
    decision = one(entry(strategy=Strategy.MOMENTUM_BREAKOUT, stop=88.0), context(mb))
    assert decision.outcome is not Outcome.REJECT
    assert approved(decision).tranches == 1


def test_heat_clip_regression_through_evaluate() -> None:
    """4 positions at 1.25% risk and a 1.25 multiplier: the 5th is clipped to 1%, not rejected."""
    held = tuple(holding(f"H{i}", seed=i) for i in range(4))
    decision = one(entry(), context(holdings=held, multiplier=1.25))
    assert rules(decision) == [GuardrailRule.HEAT]
    assert decision.outcome is Outcome.CLIP
    assert approved(decision).risk == pytest.approx(0.01)


def test_heat_with_room_for_less_than_half_a_trade() -> None:
    held = tuple(holding(f"H{i}", seed=i) for i in range(4))
    ctx = context(holdings=held, pending=(pending("P", seed=9, risk=60.0),))
    decision = one(entry(risk=0.01), ctx)  # 40 left of 100 planned
    assert rules(decision) == [GuardrailRule.HEAT]
    assert decision.outcome is Outcome.REJECT


def test_heat_reject_keeps_the_earlier_clip() -> None:
    held = tuple(holding(f"H{i}", seed=i, shares=6) for i in range(4))  # 150 each: 600
    decision = one(entry(risk=0.5), context(holdings=held))
    assert rules(decision) == [GuardrailRule.MAX_RISK, GuardrailRule.HEAT]
    assert decision.outcome is Outcome.REJECT


def test_cluster() -> None:
    held = (
        holding("A", seed=1, industry="SEMIS"),
        holding("B", seed=2, industry="SEMIS"),
    )
    assert rules(one(entry(), context(holdings=held))) == [GuardrailRule.CLUSTER]


def test_positions_limit_counts_pending() -> None:
    held = tuple(holding(f"H{i}", seed=i, shares=1) for i in range(4))
    waiting = (pending("P1", seed=11, risk=10.0), pending("P2", seed=12, risk=10.0))
    assert rules(one(entry(), context(holdings=held, pending=waiting))) == [
        GuardrailRule.MAX_POSITIONS
    ]


def test_exposure_and_no_margin() -> None:
    held = tuple(holding(f"H{i}", seed=i, shares=1, price=4500.0) for i in range(2))
    assert rules(one(entry(), context(holdings=held))) == [GuardrailRule.NO_MARGIN]
    up = context(holdings=held[:1], regime=Regime.UNDER_PRESSURE)
    assert rules(one(entry(conviction=4), up)) == [GuardrailRule.MAX_EXPOSURE]


def test_held_or_pending_symbol_is_rejected() -> None:
    ctx = context(holdings=(holding("NEW", seed=1),))
    assert rules(one(entry(), ctx)) == [GuardrailRule.TRADABLE_SYMBOL]


def test_each_approval_counts_against_the_next() -> None:
    """Two semis already pending: the first new semi fills the cluster's second slot."""
    setups = (setup("X", industry="SEMIS"), setup("Y", industry="SEMIS"), setup("Z", industry="IT"))
    ctx = context(*setups, holdings=(holding("A", seed=1, industry="SEMIS"),))
    result = evaluate([entry("X"), entry("Y"), entry("Z"), entry("X")], ctx, RISK)
    outcomes = [d.outcome for d in result.decisions]
    assert outcomes == [Outcome.APPROVE, Outcome.REJECT, Outcome.APPROVE, Outcome.REJECT]
    assert rules(result.decisions[1]) == [GuardrailRule.CLUSTER]
    assert rules(result.decisions[3]) == [GuardrailRule.TRADABLE_SYMBOL]


# --- Tranche 2, tightenings, exits --------------------------------------------------------

PASSED = GateResult(())


def test_tranche2() -> None:
    ctx = context(holdings=(holding("HELD", seed=1),))
    assert one(Tranche2("HELD", PASSED), ctx).outcome is Outcome.APPROVE
    assert rules(one(Tranche2("NOPE", PASSED), ctx)) == [GuardrailRule.TRADABLE_SYMBOL]
    failed = GateResult(("price is not in the upper part of the day's range",))
    assert rules(one(Tranche2("HELD", failed), ctx)) == [GuardrailRule.TRANCHE_2]


@pytest.mark.parametrize(
    ("ctx_kw", "holding_kw", "rule"),
    [
        ({"blocks": frozenset({EntryBlock.CRISIS_OVERRIDE})}, {}, GuardrailRule.ENTRIES_BLOCKED),
        ({"regime": Regime.CORRECTION}, {}, GuardrailRule.FUNDABLE),
        ({"regime": Regime.UNDER_PRESSURE}, {}, GuardrailRule.FUNDABLE),  # conviction 3 < 4
        ({}, {"next_report_in": 2}, GuardrailRule.EARNINGS_BLACKOUT),
        ({}, {"session": 4}, GuardrailRule.TRANCHE_2),
    ],
)
def test_tranche2_rejections(
    ctx_kw: dict[str, object], holding_kw: dict[str, object], rule: GuardrailRule
) -> None:
    ctx = context(holdings=(holding("HELD", seed=1, **holding_kw),), **ctx_kw)
    assert rules(one(Tranche2("HELD", PASSED), ctx)) == [rule]


def test_tighten() -> None:
    ctx = context(holdings=(holding("HELD", seed=1, stop=92.0),))
    ok = one(Tighten("HELD", Tightening(hard_stop=96.0), price=101.0), ctx)
    assert ok.outcome is Outcome.APPROVE
    assert isinstance(ok.approved, PositionPlan)
    assert ok.approved.hard_stop == 96.0
    loose = one(Tighten("HELD", Tightening(trail=TrailMA.SMA_50), price=101.0), ctx)
    assert loose.outcome is Outcome.APPROVE  # already the 50-day: not a loosening
    above = one(Tighten("HELD", Tightening(hard_stop=102.0), price=101.0), ctx)
    assert rules(above) == [GuardrailRule.TIGHTEN_ONLY]
    assert rules(one(Tighten("NOPE", Tightening(), price=1.0), ctx)) == [
        GuardrailRule.TRADABLE_SYMBOL
    ]


def test_exits_are_never_blocked() -> None:
    blocked = frozenset(EntryBlock)
    ctx = context(holdings=(holding("HELD", seed=1),), blocks=blocked, regime=Regime.CORRECTION)
    assert one(Exit("HELD"), ctx).outcome is Outcome.APPROVE
    assert one(Exit("HELD", 2), ctx).outcome is Outcome.APPROVE
    assert rules(one(Exit("HELD", 6), ctx)) == [GuardrailRule.INVALID_INPUT]
    assert rules(one(Exit("NOPE"), ctx)) == [GuardrailRule.TRADABLE_SYMBOL]


def test_entry_type_is_exported() -> None:
    assert isinstance(entry(), Entry)
