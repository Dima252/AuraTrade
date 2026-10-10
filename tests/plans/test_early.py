from typing import Any

import pytest

from auratrade.config import Strategy
from auratrade.plans.early import (
    below_pivot,
    heavy_volume_reversal,
    time_stop_exit,
    time_stop_halve,
)
from tests.plans.factories import PLANS, make_day, make_plan

# --- Heavy-volume reversal (O'Neil set) ---------------------------------------------------

ONEIL = make_plan(Strategy.EMERGING_LEADER)
# Session 3: a down day (99 < 101) closing below the pivot (100) on 1.6x average volume, the
# heaviest down-day volume since the breakout (1200 before).
REVERSAL: dict[str, Any] = {
    "session": 3,
    "price": 99.0,
    "prior_close": 101.0,
    "volume": 1600.0,
    "avg_volume_50": 1000.0,
    "heaviest_down_volume": 1200.0,
}


def test_heavy_volume_reversal_fires() -> None:
    assert heavy_volume_reversal(ONEIL, make_day(**REVERSAL), PLANS)
    # The first down day since the breakout is the heaviest by definition.
    assert heavy_volume_reversal(
        ONEIL, make_day(**{**REVERSAL, "heaviest_down_volume": None}), PLANS
    )
    # Exactly 1.5x counts.
    assert heavy_volume_reversal(ONEIL, make_day(**{**REVERSAL, "volume": 1500.0}), PLANS)


@pytest.mark.parametrize(
    "changes",
    [
        {"prior_close": 98.0},  # not a down day: an EP's gap day closes far above the prior close
        {"price": 100.5, "prior_close": 101.0},  # down day, but still above the pivot
        {"volume": 1499.0},  # under 1.5x the 50-day average
        {"heaviest_down_volume": 1600.0},  # not the heaviest down day (ties don't count)
        {"avg_volume_50": None},  # no average: the hard stop still protects
        {"session": 21},  # after the early window
    ],
)
def test_heavy_volume_reversal_does_not_fire(changes: dict[str, Any]) -> None:
    assert not heavy_volume_reversal(ONEIL, make_day(**{**REVERSAL, **changes}), PLANS)


def test_heavy_volume_reversal_only_in_the_plan() -> None:
    assert not heavy_volume_reversal(make_plan(Strategy.VCP_BREAKOUT), make_day(**REVERSAL), PLANS)
    vcp = make_plan(Strategy.VCP_BREAKOUT, heavy_volume_reversal=True)
    assert heavy_volume_reversal(vcp, make_day(**REVERSAL), PLANS)


# --- A dip below the pivot (every plan) -------------------------------------------------------


def test_below_pivot_in_the_early_window() -> None:
    plan = make_plan()
    assert below_pivot(plan, make_day(session=2, price=99.9), PLANS)
    assert below_pivot(plan, make_day(session=20, price=99.9), PLANS)
    assert not below_pivot(plan, make_day(session=2, price=100.0), PLANS)
    assert not below_pivot(plan, make_day(session=21, price=99.0), PLANS)


# --- Time stops (on evidence, or by Portfolio Manager choice) -------------------------------------

TIMED = make_plan(time_stop=True)


def test_time_stop_halves_at_session_10_without_progress() -> None:
    assert time_stop_halve(TIMED, make_day(session=10, price=103, high_since_entry=107.99), PLANS)
    assert not time_stop_halve(TIMED, make_day(session=9, price=103, high_since_entry=105), PLANS)
    # +1R (108) was reached at some point: progress.
    assert not time_stop_halve(TIMED, make_day(session=10, price=103, high_since_entry=108), PLANS)
    # Once only.
    halved = TIMED.evolve(time_stop_halved=True)
    assert not time_stop_halve(halved, make_day(session=12, price=103, high_since_entry=105), PLANS)


def test_time_stop_exits_at_session_20_without_progress() -> None:
    assert time_stop_exit(TIMED, make_day(session=20, price=103, high_since_entry=107), PLANS)
    assert not time_stop_exit(TIMED, make_day(session=19, price=103, high_since_entry=107), PLANS)
    assert not time_stop_exit(TIMED, make_day(session=20, price=103, high_since_entry=109), PLANS)


def test_time_stops_are_in_no_default_set() -> None:
    plan = make_plan()
    assert not time_stop_halve(plan, make_day(session=10, price=101, high_since_entry=102), PLANS)
    assert not time_stop_exit(plan, make_day(session=20, price=101, high_since_entry=102), PLANS)
