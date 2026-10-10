from auratrade.config import Regime, Strategy, TrailMA
from auratrade.plans.hold import tighten_for_downgrade, update_hold
from auratrade.plans.model import EventKind, HoldState, PositionPlan
from tests.plans.factories import PLANS, make_day, make_plan

ONEIL = make_plan(Strategy.EMERGING_LEADER)  # 8-week hold watching


def active(plan: PositionPlan = ONEIL) -> PositionPlan:
    return plan.evolve(hold=HoldState.ACTIVE, hold_until_session=40)


# --- The 8-week hold -----------------------------------------------------------------------


def test_hold_starts_on_20_percent_within_15_sessions() -> None:
    plan, events = update_hold(ONEIL, make_day(session=15, price=120.0), PLANS)
    assert plan.hold is HoldState.ACTIVE
    assert plan.hold_until_session == 40
    assert [e.kind for e in events] == [EventKind.HOLD_STARTED]
    plan, events = update_hold(ONEIL, make_day(session=7, price=119.99), PLANS)
    assert plan is ONEIL
    assert events == []


def test_hold_window_closes_after_session_15() -> None:
    plan, events = update_hold(ONEIL, make_day(session=15, price=119.0), PLANS)
    assert plan.hold is HoldState.ENDED
    assert plan.hold_trail is None
    assert events[0].details == {"reason": "not triggered in time"}
    # A +20% gain after the window doesn't start it.
    late, _ = update_hold(ONEIL, make_day(session=16, price=125.0), PLANS)
    assert late.hold is HoldState.ENDED


def test_hold_lasts_through_session_40() -> None:
    plan, events = update_hold(active(), make_day(session=40, price=130.0), PLANS)
    assert plan.hold is HoldState.ACTIVE
    assert events == []
    plan, events = update_hold(active(), make_day(session=41, price=130.0), PLANS)
    assert plan.hold is HoldState.ENDED
    assert plan.hold_until_session is None
    assert events[0].details == {"reason": "expired"}


def test_no_hold_in_the_plan_is_a_no_op() -> None:
    plan = make_plan()
    assert update_hold(plan, make_day(session=3, price=130.0), PLANS) == (plan, [])


# --- Regime-downgrade tightening -----------------------------------------------------------------


def test_downgrade_to_under_pressure_tightens_one_step() -> None:
    plan, events = tighten_for_downgrade(make_plan(), Regime.UNDER_PRESSURE)  # 50 SMA
    assert plan.exits.trail is TrailMA.EMA_21
    assert [e.kind for e in events] == [EventKind.REGIME_DOWNGRADE, EventKind.TRAIL_TIGHTENED]
    plan, _ = tighten_for_downgrade(plan, Regime.UNDER_PRESSURE)
    assert plan.exits.trail is TrailMA.TEN_DAY
    plan, events = tighten_for_downgrade(plan, Regime.UNDER_PRESSURE)
    assert plan.exits.trail is TrailMA.TEN_DAY  # nothing tighter
    assert [e.kind for e in events] == [EventKind.REGIME_DOWNGRADE]


def test_downgrade_to_under_pressure_tightens_an_active_holds_trail() -> None:
    # User decision (task 3.5a): the downgrade wins over the hold's 50-day.
    plan, _ = tighten_for_downgrade(active(), Regime.UNDER_PRESSURE)
    assert plan.hold is HoldState.ACTIVE
    assert plan.trail is TrailMA.EMA_21
    assert plan.exits.trail is TrailMA.EMA_21  # the plan's own trail too, for after the hold
    # A watching hold would start from the tightened trail.
    watching, _ = tighten_for_downgrade(ONEIL, Regime.UNDER_PRESSURE)
    assert watching.hold_trail is TrailMA.EMA_21


def test_downgrade_to_correction_uses_the_10_day_and_ends_the_hold() -> None:
    plan, events = tighten_for_downgrade(active(), Regime.CORRECTION)
    assert plan.exits.trail is TrailMA.TEN_DAY
    assert plan.hold is HoldState.ENDED
    assert plan.trail is TrailMA.TEN_DAY
    assert {e.kind for e in events} == {
        EventKind.REGIME_DOWNGRADE,
        EventKind.HOLD_ENDED,
        EventKind.TRAIL_TIGHTENED,
    }
    watching, _ = tighten_for_downgrade(ONEIL, Regime.CORRECTION)
    assert watching.hold is HoldState.ENDED


def test_downgrade_with_the_safety_rule_off_only_raises_the_event() -> None:
    plan = make_plan(regime_tightening=False)
    after, events = tighten_for_downgrade(plan, Regime.CORRECTION)
    assert after is plan
    assert [e.kind for e in events] == [EventKind.REGIME_DOWNGRADE]


def test_no_downgrade() -> None:
    plan = make_plan()
    assert tighten_for_downgrade(plan, None) == (plan, [])
