from typing import Any

import pytest
from pydantic import ValidationError

from auratrade.config import MinerviniPartial, ONeilPartial, Strategy, TrailMA
from auratrade.plans.model import HoldState, PlanChange, PositionPlan
from auratrade.plans.versioning import (
    SWITCHES,
    LooseningError,
    Tightening,
    check_tighten_only,
    loosenings,
    next_version,
    scale_for_split,
    tighten,
    with_tranche2,
)
from tests.plans.factories import PLANS, make_plan


def exits(plan: PositionPlan, **changes: Any) -> PositionPlan:
    return plan.evolve(exits=plan.exits.model_copy(update=changes))


# --- Loosening is rejected -----------------------------------------------------------------


def test_lowering_the_hard_stop_is_rejected() -> None:
    plan = make_plan().evolve(hard_stop=100.0)
    with pytest.raises(LooseningError, match="hard stop cannot be lowered"):
        check_tighten_only(plan, plan.evolve(hard_stop=99.99))


def test_widening_the_trail_is_rejected() -> None:
    plan = make_plan(trail=TrailMA.EMA_21)
    with pytest.raises(LooseningError, match="trail cannot widen"):
        check_tighten_only(plan, exits(plan, trail=TrailMA.SMA_50))
    check_tighten_only(plan, exits(plan, trail=TrailMA.TEN_DAY))


def test_partial_changes() -> None:
    plan = make_plan()  # Minervini: ⅓ at +2R
    assert isinstance(plan.exits.partial, MinerviniPartial)
    later = plan.exits.partial.model_copy(update={"r_multiple": 3.0})
    less = plan.exits.partial.model_copy(update={"fraction": 0.25})
    more_sooner = plan.exits.partial.model_copy(update={"fraction": 0.5})
    oneil = ONeilPartial(fraction=0.5, gain=0.2)
    assert loosenings(plan, exits(plan, partial=later)) == [
        "the strength partial cannot sell later"
    ]
    assert loosenings(plan, exits(plan, partial=less)) == [
        "the strength partial cannot sell less (0.3333333333 -> 0.25)"
    ]
    assert "cannot be removed" in loosenings(plan, exits(plan, partial=None))[0]
    assert (
        "cannot change from MINERVINI to ONEIL" in loosenings(plan, exits(plan, partial=oneil))[0]
    )
    check_tighten_only(plan, exits(plan, partial=more_sooner))
    # Adding a partial to a plan without one sells earlier.
    bare = make_plan(partial=None)
    check_tighten_only(bare, exits(bare, partial=oneil))


def test_kullamagi_and_oneil_partials_cannot_sell_later() -> None:
    momentum = make_plan(Strategy.MOMENTUM_BREAKOUT)  # after 3 sessions
    assert momentum.exits.partial is not None
    later = momentum.exits.partial.model_copy(update={"after_sessions": 4})
    assert loosenings(momentum, exits(momentum, partial=later))
    emerging = make_plan(Strategy.EMERGING_LEADER)  # at +20%
    assert emerging.exits.partial is not None
    higher = emerging.exits.partial.model_copy(update={"gain": 0.25})
    assert loosenings(emerging, exits(emerging, partial=higher))


@pytest.mark.parametrize("switch", SWITCHES)
def test_switching_a_rule_off_is_rejected(switch: str) -> None:
    plan = exits(make_plan(), **{switch: True})
    with pytest.raises(LooseningError, match=f"{switch} cannot be switched off"):
        check_tighten_only(plan, exits(plan, **{switch: False}))
    check_tighten_only(exits(plan, **{switch: False}), plan)  # switching on is tighter


def test_an_8_week_hold_cannot_be_added_after_entry() -> None:
    plan = make_plan()
    later = plan.evolve(
        exits=plan.exits.model_copy(update={"eight_week_hold": True}),
        hold=HoldState.WATCHING,
        hold_trail=TrailMA.SMA_50,
    )
    violations = loosenings(plan, later)
    assert "an 8-week hold can only be chosen at entry" in violations
    assert "an 8-week-hold trail can only be set at entry" in violations
    assert "the 8-week hold cannot go from OFF to WATCHING" in violations


def test_hold_moves_forward_only() -> None:
    watching = make_plan(Strategy.EMERGING_LEADER)
    active = watching.evolve(hold=HoldState.ACTIVE, hold_until_session=40)
    ended = watching.evolve(hold=HoldState.ENDED, hold_trail=None)
    check_tighten_only(watching, active)  # committed at entry
    check_tighten_only(watching, ended)
    check_tighten_only(active, ended)
    assert loosenings(active, watching) == ["the 8-week hold cannot go from ACTIVE to WATCHING"]
    assert "cannot go from ENDED to ACTIVE" in loosenings(ended, active)[0]
    assert loosenings(active, active.evolve(hold_until_session=45)) == [
        "the 8-week hold cannot be extended"
    ]
    tighter = active.evolve(hold_trail=TrailMA.EMA_21)
    assert loosenings(tighter, active) == ["the 8-week-hold trail cannot widen (EMA_21 -> SMA_50)"]


@pytest.mark.parametrize(
    "milestone", ["partial_taken", "climax_half_taken", "late_stage_taken", "time_stop_halved"]
)
def test_milestones_cannot_be_undone(milestone: str) -> None:
    plan = make_plan().evolve(**{milestone: True})
    assert loosenings(plan, plan.evolve(**{milestone: False})) == [f"{milestone} cannot be undone"]


@pytest.mark.parametrize(
    "changes",
    [
        {"pivot": 99.0},
        {"entry_price": 101.0},
        {"initial_stop": 91.0},
        {"strategy": Strategy.LEADER_PULLBACK},
        {"tranche2_filled": True},
    ],
)
def test_entry_facts_cannot_change(changes: dict[str, Any]) -> None:
    plan = make_plan()
    with pytest.raises(LooseningError, match="cannot change"):
        check_tighten_only(plan, plan.evolve(**changes))


def test_every_violation_is_reported() -> None:
    plan = make_plan(trail=TrailMA.EMA_21, round_trip=True).evolve(hard_stop=95.0)
    worse = exits(plan.evolve(hard_stop=94.0), trail=TrailMA.SMA_50, round_trip=False)
    with pytest.raises(LooseningError) as caught:
        check_tighten_only(plan, worse)
    assert len(caught.value.violations) == 3


def test_next_version() -> None:
    plan = make_plan()
    raised = next_version(plan, plan.evolve(hard_stop=95.0), PlanChange.MILESTONE)
    assert (raised.version, raised.change, raised.hard_stop) == (2, PlanChange.MILESTONE, 95.0)
    with pytest.raises(LooseningError):
        next_version(raised, plan, PlanChange.TIGHTEN)


# --- LLM / Portfolio Manager tightenings -------------------------------------------------------


def test_tighten_raises_the_stop_rounded_up_to_a_tick() -> None:
    plan = tighten(make_plan(), Tightening(hard_stop=96.123), price=105.0, config=PLANS)
    assert (plan.version, plan.change, plan.hard_stop) == (2, PlanChange.TIGHTEN, 96.13)


@pytest.mark.parametrize("stop", [105.0, 104.999, 110.0])
def test_a_stop_at_or_above_the_price_is_not_a_tightening(stop: float) -> None:
    with pytest.raises(LooseningError, match="exit instead"):
        tighten(make_plan(), Tightening(hard_stop=stop), price=105.0, config=PLANS)


def test_tighten_cannot_lower_the_stop() -> None:
    plan = make_plan().evolve(hard_stop=100.0)
    with pytest.raises(LooseningError, match="lowered"):
        tighten(plan, Tightening(hard_stop=98.0), price=105.0, config=PLANS)


@pytest.mark.parametrize("stop", [float("nan"), float("inf"), 0.0, -3.0])
def test_tighten_rejects_unusable_stop_values(stop: float) -> None:
    with pytest.raises(ValidationError):
        Tightening(hard_stop=stop)


def test_tighten_rejects_a_bad_price() -> None:
    with pytest.raises(ValueError, match="price"):
        tighten(make_plan(), Tightening(), price=float("nan"), config=PLANS)


def test_tighten_menu_choices() -> None:
    plan = make_plan()
    tightened = tighten(
        plan,
        Tightening(
            trail=TrailMA.TEN_DAY,
            partial=MinerviniPartial(fraction=0.5, r_multiple=2.0),
            switch_on=frozenset({"time_stop", "round_trip"}),
        ),
        price=104.0,
        config=PLANS,
    )
    assert tightened.exits.trail is TrailMA.TEN_DAY
    assert tightened.exits.time_stop
    assert tightened.exits.round_trip
    assert tightened.exits.partial == MinerviniPartial(fraction=0.5, r_multiple=2.0)
    with pytest.raises(LooseningError, match="outside"):
        tighten(
            plan,
            Tightening(partial=MinerviniPartial(fraction=0.5, r_multiple=1.0)),
            price=104.0,
            config=PLANS,
        )
    with pytest.raises(LooseningError, match="trail cannot widen"):
        tighten(tightened, Tightening(trail=TrailMA.SMA_50), price=104.0, config=PLANS)
    with pytest.raises(LooseningError, match="removed"):
        tighten(plan, Tightening(partial=None), price=104.0, config=PLANS)


def test_tighten_cannot_switch_on_an_8_week_hold() -> None:
    with pytest.raises(ValidationError):
        Tightening(switch_on=frozenset({"eight_week_hold"}))  # type: ignore[arg-type]


def test_tighten_can_end_an_8_week_hold() -> None:
    active = make_plan(Strategy.EMERGING_LEADER).evolve(
        hold=HoldState.ACTIVE, hold_until_session=40
    )
    ended = tighten(active, Tightening(end_eight_week_hold=True), price=125.0, config=PLANS)
    assert ended.hold is HoldState.ENDED
    assert ended.hold_trail is None
    # Nothing to end on a plan without one.
    assert (
        tighten(make_plan(), Tightening(end_eight_week_hold=True), price=101, config=PLANS).hold
        is HoldState.OFF
    )


# --- Committed transitions: tranche 2 and splits -------------------------------------------------


def test_tranche2_updates_the_average_entry() -> None:
    plan = with_tranche2(make_plan(), entry_price=101.5)
    assert (plan.version, plan.change, plan.entry_price) == (2, PlanChange.TRANCHE_2, 101.5)
    assert plan.tranche2_filled
    assert plan.hard_stop == 92.0
    with pytest.raises(ValueError, match="once"):
        with_tranche2(plan, entry_price=102.0)
    with pytest.raises(ValueError, match="once"):
        with_tranche2(make_plan(Strategy.MOMENTUM_BREAKOUT), entry_price=102.0)
    with pytest.raises(ValueError, match="above the initial stop"):
        with_tranche2(make_plan(), entry_price=91.0)


def test_split_scales_every_level() -> None:
    plan = make_plan().evolve(hard_stop=95.005)
    split = scale_for_split(plan, 2.0)
    assert (split.version, split.change) == (2, PlanChange.SPLIT)
    assert (split.pivot, split.entry_price, split.initial_stop) == (50.0, 50.0, 46.0)
    assert split.hard_stop == 47.51  # 47.5025 rounded up
    reverse = scale_for_split(make_plan(), 0.1)
    assert (reverse.entry_price, reverse.hard_stop) == pytest.approx((1000.0, 920.0))
    with pytest.raises(ValueError, match="ratio"):
        scale_for_split(plan, 0.0)
