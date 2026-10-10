import pytest

from auratrade.config import Strategy, TrailMA
from auratrade.plans.levels import plan_levels, trail_levels
from auratrade.plans.model import HoldState
from tests.plans.factories import LOW_TRAILS, PLANS, make_plan


def test_trail_levels() -> None:
    closes = [float(c) for c in range(1, 61)]  # 1 .. 60
    levels = trail_levels(closes, PLANS)
    assert levels[TrailMA.TEN_DAY] == pytest.approx(55.5)  # mean of 51 .. 60
    assert levels[TrailMA.SMA_50] == pytest.approx(35.5)  # mean of 11 .. 60
    # A 21-session EMA of a straight line lags it by (21 - 1) / 2 = 10 sessions.
    assert levels[TrailMA.EMA_21] == pytest.approx(50.0)


def test_trail_levels_warm_up() -> None:
    levels = trail_levels([10.0] * 20, PLANS)
    assert levels[TrailMA.TEN_DAY] == 10.0
    assert levels[TrailMA.EMA_21] is None
    assert levels[TrailMA.SMA_50] is None
    assert trail_levels([], PLANS) == dict.fromkeys(TrailMA)


def test_minervini_levels() -> None:
    levels = plan_levels(make_plan(), LOW_TRAILS, PLANS)
    assert (levels.hard_stop, levels.breakeven) == (92.0, 100.0)
    assert (levels.trail, levels.trail_level) == (TrailMA.SMA_50, 50.0)
    assert levels.breakeven_trigger == 124.0  # +3R
    assert levels.round_trip_trigger is None  # not in the Minervini set
    assert levels.partial_trigger == 116.0  # +2R


def test_oneil_and_kullamagi_levels() -> None:
    oneil = plan_levels(make_plan(Strategy.EMERGING_LEADER), LOW_TRAILS, PLANS)
    assert oneil.round_trip_trigger == pytest.approx(110.0)
    assert oneil.breakeven_trigger is None
    assert oneil.partial_trigger == pytest.approx(120.0)
    kullamagi = plan_levels(make_plan(Strategy.MOMENTUM_BREAKOUT), LOW_TRAILS, PLANS)
    assert kullamagi.partial_trigger == 112.0  # +1.5R at session 3
    assert kullamagi.trail is TrailMA.TEN_DAY


def test_reached_milestones_and_partials_drop_out() -> None:
    plan = make_plan().evolve(hard_stop=100.0, partial_taken=True)
    levels = plan_levels(plan, LOW_TRAILS, PLANS)
    assert levels.breakeven_trigger is None
    assert levels.partial_trigger is None
    assert plan_levels(make_plan(partial=None), LOW_TRAILS, PLANS).partial_trigger is None


def test_an_active_hold_suspends_the_partial_and_trails_the_50_day() -> None:
    plan = make_plan(Strategy.MOMENTUM_BREAKOUT, eight_week_hold=True).evolve(
        hold=HoldState.ACTIVE, hold_until_session=40
    )
    levels = plan_levels(plan, {**LOW_TRAILS, TrailMA.SMA_50: 88.0}, PLANS)
    assert levels.partial_trigger is None
    assert (levels.trail, levels.trail_level) == (TrailMA.SMA_50, 88.0)
