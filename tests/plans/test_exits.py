from auratrade.config import Strategy, TrailMA
from auratrade.plans.exits import (
    ClimaxSale,
    climax_sale,
    late_stage_sale,
    milestone_stop,
    strength_partial,
    trail_exit,
)
from auratrade.plans.model import HoldState
from tests.plans.factories import PLANS, RISK, make_day, make_plan

# --- Strength partials (decision price) ----------------------------------------------------


def test_kullamagi_partial_at_session_n_if_at_least_1_5r() -> None:
    plan = make_plan(Strategy.MOMENTUM_BREAKOUT)  # after 3 sessions, +1.5R = 112
    assert strength_partial(plan, make_day(session=3, price=112.0), PLANS)
    assert not strength_partial(plan, make_day(session=3, price=111.99), PLANS)
    assert not strength_partial(plan, make_day(session=2, price=120.0), PLANS)
    assert not strength_partial(plan, make_day(session=4, price=120.0), PLANS)


def test_oneil_partial_at_20_percent() -> None:
    plan = make_plan(Strategy.EMERGING_LEADER)
    assert strength_partial(plan, make_day(session=8, price=120.0), PLANS)
    assert not strength_partial(plan, make_day(session=8, price=119.99), PLANS)


def test_minervini_partial_at_2r() -> None:
    plan = make_plan(Strategy.VCP_BREAKOUT)
    assert strength_partial(plan, make_day(session=8, price=116.0), PLANS)
    assert not strength_partial(plan, make_day(session=8, price=115.99), PLANS)


def test_strength_partial_once_and_only_in_the_plan() -> None:
    plan = make_plan()
    assert not strength_partial(plan.evolve(partial_taken=True), make_day(price=130.0), PLANS)
    assert not strength_partial(make_plan(partial=None), make_day(price=130.0), PLANS)


# --- Climax sales (O'Neil set) -----------------------------------------------------------------


def test_climax_sales() -> None:
    plan = make_plan(Strategy.EMERGING_LEADER)
    climax = RISK.climax
    assert climax_sale(plan, make_day(climax_signals=1), climax) is None
    assert climax_sale(plan, make_day(climax_signals=2), climax) is ClimaxSale.HALF
    assert climax_sale(plan, make_day(climax_signals=3), climax) is ClimaxSale.ALL
    halved = plan.evolve(climax_half_taken=True)
    assert climax_sale(halved, make_day(climax_signals=2), climax) is None  # ½ once
    assert climax_sale(halved, make_day(climax_signals=4), climax) is ClimaxSale.ALL
    assert climax_sale(make_plan(), make_day(climax_signals=3), climax) is None  # not in the plan


# --- Late-stage base sale (O'Neil set) ------------------------------------------------------------


def test_late_stage_sale_on_a_4th_stage_breakout() -> None:
    plan = make_plan(Strategy.EMERGING_LEADER)
    assert late_stage_sale(plan, make_day(breakout_base_count=4), PLANS)
    assert late_stage_sale(plan, make_day(breakout_base_count=5), PLANS)
    assert not late_stage_sale(plan, make_day(breakout_base_count=3), PLANS)
    assert not late_stage_sale(plan, make_day(), PLANS)
    assert not late_stage_sale(
        plan.evolve(late_stage_taken=True), make_day(breakout_base_count=4), PLANS
    )
    assert not late_stage_sale(make_plan(), make_day(breakout_base_count=4), PLANS)


# --- Trails ------------------------------------------------------------------------------------


def test_trail_exit_on_the_first_close_below() -> None:
    plan = make_plan()  # 50 SMA
    assert trail_exit(plan, make_day(price=104.9, trails={TrailMA.SMA_50: 105.0}))
    assert not trail_exit(plan, make_day(price=105.0, trails={TrailMA.SMA_50: 105.0}))
    # Only the trail in force counts.
    assert not trail_exit(plan, make_day(price=104.0, trails={TrailMA.TEN_DAY: 106.0}))
    # Still warming up: no level, no exit.
    assert not trail_exit(plan, make_day(price=104.0, trails={TrailMA.SMA_50: None}))


# --- Stop-raising milestones (session high) -----------------------------------------------------


def test_breakeven_at_3r() -> None:
    plan = make_plan()  # Minervini: breakeven at +3R = 124
    assert milestone_stop(plan, make_day(price=118, high_since_entry=124.0), PLANS) == 100.0
    assert milestone_stop(plan, make_day(price=118, high_since_entry=123.9), PLANS) == 92.0
    no_rule = make_plan(breakeven_at_r=False)
    assert milestone_stop(no_rule, make_day(price=118, high_since_entry=130), PLANS) == 92.0


def test_round_trip_rule_at_10_percent() -> None:
    plan = make_plan(Strategy.EMERGING_LEADER)
    assert milestone_stop(plan, make_day(price=104, high_since_entry=110.0), PLANS) == 100.0
    assert milestone_stop(plan, make_day(price=104, high_since_entry=109.9), PLANS) == 92.0
    # In O'Neil's set only, unless added on evidence.
    momentum = make_plan(Strategy.MOMENTUM_BREAKOUT)
    assert milestone_stop(momentum, make_day(price=104, high_since_entry=115), PLANS) == 92.0


def test_an_active_hold_keeps_the_stop_at_breakeven() -> None:
    active = make_plan(Strategy.EMERGING_LEADER).evolve(
        hold=HoldState.ACTIVE, hold_until_session=40
    )
    assert milestone_stop(active, make_day(price=104, high_since_entry=105), PLANS) == 100.0


def test_milestones_never_lower_the_stop() -> None:
    plan = make_plan().evolve(hard_stop=103.0)
    assert milestone_stop(plan, make_day(price=126, high_since_entry=130), PLANS) == 103.0
