from dataclasses import replace as _with
from typing import Any

import pytest

from auratrade.config import (
    EntryPolicy,
    GateKind,
    KullamagiPartial,
    MinerviniPartial,
    ONeilPartial,
    StopKind,
    Strategy,
    TrailMA,
    TriggerKind,
)
from auratrade.plans.entry import (
    GateInput,
    PlanError,
    confirmation_gate,
    entry_policy,
    new_plan,
    tranche2_gate,
)
from auratrade.plans.model import HOLD_TRAIL, HoldState, PlanChange
from tests.plans.factories import PLANS, exits_for, make_plan

# --- Entry policies (README §7.1) ----------------------------------------------------------


def test_entry_policies_match_the_readme() -> None:
    vcp = entry_policy(Strategy.VCP_BREAKOUT, PLANS)
    assert (vcp.trigger, vcp.stop, vcp.tranches, vcp.gate) == (
        TriggerKind.PIVOT,
        StopKind.STRUCTURAL,
        2,
        GateKind.BREAKOUT,
    )
    assert entry_policy(Strategy.EMERGING_LEADER, PLANS).gate is GateKind.BREAKOUT
    assert entry_policy(Strategy.LEADER_PULLBACK, PLANS).gate is GateKind.PULLBACK
    momentum = entry_policy(Strategy.MOMENTUM_BREAKOUT, PLANS)
    assert (momentum.trigger, momentum.stop, momentum.tranches, momentum.gate) == (
        TriggerKind.CONSOLIDATION_OR_RANGE_HIGH,
        StopKind.LOW_OF_DAY,
        1,
        None,
    )
    ep = entry_policy(Strategy.EPISODIC_PIVOT, PLANS)
    assert (ep.trigger, ep.tranches) == (TriggerKind.OPENING_RANGE_HIGH, 1)


# --- Default exit sets (README §7.3) -------------------------------------------------------


@pytest.mark.parametrize("strategy", [Strategy.MOMENTUM_BREAKOUT, Strategy.EPISODIC_PIVOT])
def test_kullamagi_set(strategy: Strategy) -> None:
    exits = make_plan(strategy).exits
    assert isinstance(exits.partial, KullamagiPartial)
    assert exits.breakeven_after_partial
    assert not exits.breakeven_at_r
    assert exits.trail is TrailMA.TEN_DAY
    assert not (exits.round_trip)
    assert not (exits.eight_week_hold)
    assert not (exits.climax_sales)
    assert not (exits.heavy_volume_reversal)
    assert not (exits.late_stage_sale)
    assert not (exits.time_stop)


@pytest.mark.parametrize("strategy", [Strategy.VCP_BREAKOUT, Strategy.LEADER_PULLBACK])
def test_minervini_set(strategy: Strategy) -> None:
    exits = make_plan(strategy).exits
    assert isinstance(exits.partial, MinerviniPartial)
    assert exits.breakeven_after_partial
    assert exits.breakeven_at_r
    assert exits.trail is TrailMA.SMA_50
    assert not (exits.round_trip)
    assert not (exits.eight_week_hold)
    assert not (exits.climax_sales)
    assert not (exits.heavy_volume_reversal)
    assert not (exits.late_stage_sale)
    assert not (exits.time_stop)


def test_oneil_set() -> None:
    exits = make_plan(Strategy.EMERGING_LEADER).exits
    assert isinstance(exits.partial, ONeilPartial)
    assert not (exits.breakeven_after_partial)
    assert not (exits.breakeven_at_r)
    assert exits.trail is TrailMA.SMA_50
    assert exits.round_trip
    assert exits.eight_week_hold
    assert exits.climax_sales
    assert exits.heavy_volume_reversal
    assert exits.late_stage_sale
    assert not exits.time_stop  # time stops are in no default set


@pytest.mark.parametrize("strategy", list(Strategy))
def test_every_default_plan_has_the_safety_rules(strategy: Strategy) -> None:
    exits = make_plan(strategy).exits
    assert exits.earnings_cushion
    assert exits.regime_tightening


def test_overrides_switch_single_rules() -> None:
    raw = PLANS.model_dump()
    raw["default_plans"]["VCP_BREAKOUT"]["overrides"] = {"round_trip": True, "partial": None}
    config = type(PLANS).model_validate(raw)
    exits = config.default_exit_plan(Strategy.VCP_BREAKOUT)
    assert exits.round_trip
    assert exits.partial is None
    assert exits.trail is TrailMA.SMA_50  # the rest of the set is kept


# --- The plan written at entry -------------------------------------------------------------


def test_new_plan() -> None:
    plan = new_plan(
        Strategy.EMERGING_LEADER, pivot=50.0, entry_price=50.5, initial_stop=46.5, config=PLANS
    )
    assert (plan.version, plan.change) == (1, PlanChange.ENTRY)
    assert plan.hard_stop == plan.initial_stop == 46.5
    assert plan.hold is HoldState.WATCHING
    assert plan.hold_trail is HOLD_TRAIL
    assert not (plan.partial_taken)
    assert not (plan.tranche2_filled)
    assert make_plan(Strategy.VCP_BREAKOUT).hold is HoldState.OFF


def test_portfolio_manager_menu_choice_is_kept() -> None:
    exits = exits_for(Strategy.VCP_BREAKOUT, trail=TrailMA.EMA_21, time_stop=True)
    plan = new_plan(
        Strategy.VCP_BREAKOUT,
        pivot=100,
        entry_price=100,
        initial_stop=92,
        config=PLANS,
        exits=exits,
    )
    assert plan.exits == exits


@pytest.mark.parametrize(
    "partial",
    [
        {"kind": "MINERVINI", "fraction": 0.3333333333, "r_multiple": 1.5},  # below +2R
        {"kind": "MINERVINI", "fraction": 0.75, "r_multiple": 2.5},  # more than ½
        {"kind": "ONEIL", "fraction": 0.25, "gain": 0.30},  # beyond +25%
        {"kind": "KULLAMAGI", "fraction": 0.5, "after_sessions": 2},  # before session 3
    ],
)
def test_menu_values_out_of_bounds_are_rejected(partial: dict[str, Any]) -> None:
    with pytest.raises(PlanError, match="outside"):
        make_plan(partial=partial)


@pytest.mark.parametrize(
    ("levels", "error"),
    [
        ({"entry": 100.0, "stop": 100.0}, "below the entry"),
        ({"entry": 100.0, "stop": -1.0}, "initial_stop"),
        ({"pivot": float("nan")}, "pivot"),
    ],
)
def test_levels_are_checked(levels: dict[str, float], error: str) -> None:
    with pytest.raises(PlanError, match=error):
        make_plan(**levels)  # type: ignore[arg-type]


# --- Tranche-2 confirmation gates ------------------------------------------------------------

BREAKOUT_OK = GateInput(
    price=102.0,
    level=100.0,
    day_high=103.0,
    day_low=99.0,  # (102 - 99) / 4 = 0.75 of the range
    projected_volume=1500.0,  # 1.5x
    avg_volume_50=1000.0,
    rs_line_near_high=True,
    rs_line_at_low=False,
)
PULLBACK_OK = GateInput(
    price=51.0,
    level=50.0,
    day_high=51.5,
    day_low=49.5,
    projected_volume=1000.0,  # 1.0x
    avg_volume_50=1000.0,
    rs_line_near_high=False,
    rs_line_at_low=False,
    tested_ma=49.0,
)


def test_breakout_gate_passes() -> None:
    assert confirmation_gate(GateKind.BREAKOUT, BREAKOUT_OK, PLANS).passed
    assert confirmation_gate(
        GateKind.BREAKOUT, _with(BREAKOUT_OK, price=105.0, day_high=105.0), PLANS
    ).passed


@pytest.mark.parametrize(
    ("changes", "failure"),
    [
        ({"price": 99.9, "day_low": 98.0}, "below the level"),  # back below the pivot
        ({"price": 105.1, "day_high": 106.0}, "max chase"),  # past the max chase
        ({"day_low": 101.0, "day_high": 105.0}, "upper"),  # (102 - 101) / 4 = 0.25
        ({"day_high": 99.0}, "upper"),  # no range
        ({"projected_volume": 1399.0}, "volume"),  # < 1.4x
        ({"avg_volume_50": None}, "volume"),
        ({"rs_line_near_high": False}, "RS line is not"),
    ],
)
def test_breakout_gate_fails(changes: dict[str, Any], failure: str) -> None:
    result = confirmation_gate(GateKind.BREAKOUT, _with(BREAKOUT_OK, **changes), PLANS)
    assert not result.passed
    assert any(failure in f for f in result.failures)


def test_pullback_gate_passes_without_breakout_volume_or_a_new_rs_high() -> None:
    assert confirmation_gate(GateKind.PULLBACK, PULLBACK_OK, PLANS).passed
    # The breakout gate would reject the same session.
    assert not confirmation_gate(GateKind.BREAKOUT, PULLBACK_OK, PLANS).passed


@pytest.mark.parametrize(
    ("changes", "failure"),
    [
        ({"price": 52.6, "day_high": 53.0}, "max chase"),
        ({"projected_volume": 999.0}, "volume"),
        ({"rs_line_at_low": True}, "50-session low"),
        ({"tested_ma": 51.0}, "tested moving average"),
        ({"tested_ma": None}, "tested moving average"),
    ],
)
def test_pullback_gate_fails(changes: dict[str, Any], failure: str) -> None:
    result = confirmation_gate(GateKind.PULLBACK, _with(PULLBACK_OK, **changes), PLANS)
    assert any(failure in f for f in result.failures)


def test_gate_rejects_non_finite_prices() -> None:
    result = confirmation_gate(GateKind.BREAKOUT, _with(BREAKOUT_OK, price=float("nan")), PLANS)
    assert result.failures == ("prices must be finite",)


def test_tranche2_gate_per_strategy() -> None:
    assert tranche2_gate(Strategy.VCP_BREAKOUT, BREAKOUT_OK, PLANS).passed
    assert tranche2_gate(Strategy.EMERGING_LEADER, BREAKOUT_OK, PLANS).passed
    assert tranche2_gate(Strategy.LEADER_PULLBACK, PULLBACK_OK, PLANS).passed
    for single in (Strategy.MOMENTUM_BREAKOUT, Strategy.EPISODIC_PIVOT):
        result = tranche2_gate(single, BREAKOUT_OK, PLANS)
        assert not result.passed
        assert "single tranche" in result.failures[0]


# --- The close-entry candidate (#144) ----------------------------------------------------------

GAP_OK = GateInput(
    price=112.0,
    level=110.0,  # the gap session's open
    day_high=114.0,
    day_low=106.0,
    projected_volume=3000.0,  # 3x
    avg_volume_50=1000.0,
    rs_line_near_high=False,
    rs_line_at_low=True,
)


def test_gap_held_gate_has_no_max_chase_and_no_rs_conditions() -> None:
    assert confirmation_gate(GateKind.GAP_HELD, GAP_OK, PLANS).passed
    far = _with(GAP_OK, price=125.0, day_high=126.0)  # 14% above the open
    assert confirmation_gate(GateKind.GAP_HELD, far, PLANS).passed


@pytest.mark.parametrize(
    ("changes", "failure"),
    [
        ({"price": 109.9}, "below the level"),  # the gap didn't hold
        ({"price": 109.0, "day_low": 105.0}, "below the level"),
        ({"day_high": 125.0}, "upper"),  # (112 - 106) / 19 < 0.5
        ({"projected_volume": 2999.0}, "volume"),  # < 3x, the after-close EP rule
    ],
)
def test_gap_held_gate_fails(changes: dict[str, Any], failure: str) -> None:
    result = confirmation_gate(GateKind.GAP_HELD, _with(GAP_OK, **changes), PLANS)
    assert any(failure in f for f in result.failures)


def test_gate_without_its_volume_condition() -> None:
    gates = dict(PLANS.confirmation_gates)
    gates[GateKind.BREAKOUT] = gates[GateKind.BREAKOUT].model_copy(
        update={"min_volume_ratio": None}
    )
    plans = PLANS.model_copy(update={"confirmation_gates": gates})
    light = _with(BREAKOUT_OK, projected_volume=1.0, avg_volume_50=None)
    assert confirmation_gate(GateKind.BREAKOUT, light, plans).passed
    assert not confirmation_gate(GateKind.BREAKOUT, light, PLANS).passed


def test_close_candidate_matches_the_preregistration() -> None:
    candidate = PLANS.entry_candidates["CLOSE"]
    gates = {
        Strategy.VCP_BREAKOUT: GateKind.BREAKOUT,
        Strategy.EMERGING_LEADER: GateKind.BREAKOUT,
        Strategy.MOMENTUM_BREAKOUT: GateKind.BREAKOUT,
        Strategy.LEADER_PULLBACK: GateKind.PULLBACK,
        Strategy.EPISODIC_PIVOT: GateKind.GAP_HELD,
    }
    for strategy, gate in gates.items():
        policy = candidate.entry[strategy]
        assert (policy.trigger, policy.stop, policy.tranches, policy.gate) == (
            TriggerKind.CLOSE,
            StopKind.TRIGGER_DAY_LOW,
            1,
            gate,
        )
    ep = candidate.stop_widths[Strategy.EPISODIC_PIVOT]
    assert (ep.unit.value, ep.max) == ("LIMIT_PCT", 0.08)
    gap = PLANS.confirmation_gates[GateKind.GAP_HELD]
    assert gap.max_above_level is None
    assert (gap.min_range_position, gap.min_volume_ratio) == (0.5, 3.0)


@pytest.mark.parametrize(
    "fields",
    [
        {"trigger": "CLOSE", "stop": "TRIGGER_DAY_LOW", "tranches": 2, "gate": "BREAKOUT"},
        {"trigger": "CLOSE", "stop": "TRIGGER_DAY_LOW", "tranches": 1},
    ],
)
def test_close_entry_policy_is_one_tranche_with_a_gate(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="close entry"):
        EntryPolicy.model_validate(fields)
