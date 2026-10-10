import math

import pytest
from pydantic import ValidationError

from auratrade.config import Strategy, TrailMA
from auratrade.plans.model import (
    HoldState,
    Rule,
    shares_to_sell,
    stop_tick_up,
    tighter_trail,
)
from tests.plans.factories import make_day, make_plan


def test_rules_are_in_readme_precedence_order() -> None:
    assert list(Rule) == [
        Rule.HARD_STOP,
        Rule.EARNINGS_CUSHION,
        Rule.HEAVY_VOLUME_REVERSAL,
        Rule.CLIMAX,
        Rule.LATE_STAGE,
        Rule.REGIME_DOWNGRADE,
        Rule.EIGHT_WEEK_HOLD,
        Rule.STRENGTH_PARTIAL,
        Rule.TRAIL,
        Rule.TIME_STOP,
    ]


def test_trail_steps() -> None:
    assert tighter_trail(TrailMA.SMA_50) is TrailMA.EMA_21
    assert tighter_trail(TrailMA.EMA_21) is TrailMA.TEN_DAY
    assert tighter_trail(TrailMA.TEN_DAY) is TrailMA.TEN_DAY


@pytest.mark.parametrize(
    ("price", "tick"),
    [(100.0, 100.0), (100.001, 100.01), (10.0 / 3, 3.34), (0.12341, 0.1235), (0.5, 0.5)],
)
def test_stop_tick_up(price: float, tick: float) -> None:
    assert stop_tick_up(price) == tick


@pytest.mark.parametrize(
    ("shares", "fraction", "sold"),
    [(30, 0.3333333333, 10), (100, 0.5, 50), (1, 0.5, 0), (3, 0.3333333333, 1), (7, 0.25, 1)],
)
def test_shares_to_sell_floors(shares: int, fraction: float, sold: int) -> None:
    assert shares_to_sell(shares, fraction) == sold


def test_plan_properties() -> None:
    plan = make_plan(entry=100.005)
    assert plan.risk_per_share == pytest.approx(8.005)
    assert plan.breakeven == 100.01  # rounded up, never undercut
    assert plan.gain_r(116.015) == pytest.approx(2.0)
    assert plan.trail is TrailMA.SMA_50


def test_trail_in_force_during_an_active_hold() -> None:
    plan = make_plan(Strategy.MOMENTUM_BREAKOUT, eight_week_hold=True)
    assert plan.hold is HoldState.WATCHING
    assert plan.trail is TrailMA.TEN_DAY
    active = plan.evolve(hold=HoldState.ACTIVE, hold_until_session=40)
    assert active.trail is TrailMA.SMA_50


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"initial_stop": 100.0}, "below the entry"),
        ({"hard_stop": 91.0}, "below the initial stop"),
        ({"hold": HoldState.WATCHING}, "OFF exactly"),
        ({"hold_trail": TrailMA.SMA_50}, "hold_trail"),
        ({"hold_until_session": 40}, "hold_until_session"),
        ({"pivot": math.nan}, "pivot"),
        ({"entry_price": math.inf}, "entry_price"),
        ({"hard_stop": -1.0}, "hard_stop"),
    ],
)
def test_invalid_plans(changes: dict[str, object], error: str) -> None:
    with pytest.raises(ValidationError, match=error):
        make_plan().evolve(**changes)


def test_tranche2_needs_a_two_tranche_entry() -> None:
    with pytest.raises(ValidationError, match="single-tranche"):
        make_plan(Strategy.MOMENTUM_BREAKOUT).evolve(tranche2_filled=True)


def test_plans_are_immutable() -> None:
    with pytest.raises(ValidationError):
        make_plan().hard_stop = 99.0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"session": 0}, "session"),
        ({"price": math.nan}, "price"),
        ({"low": 0.0}, "low"),
        ({"avg_volume_50": -5.0}, "avg_volume_50"),
        ({"volume": -1.0}, "volume"),
        ({"climax_signals": -1}, "climax_signals"),
    ],
)
def test_invalid_days(changes: dict[str, object], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        make_day(**changes)  # type: ignore[arg-type]
