"""The breakout tracker (journal.breakout_tracker)."""

from datetime import date, timedelta

import pytest

from auratrade.config import Strategy, load_risk_config
from auratrade.indicators.bars import Bar
from auratrade.journal.breakout_tracker import (
    Breakout,
    Tracked,
    Watched,
    above_trigger,
    for_pillar,
    triggers,
)
from auratrade.regime.pillars import TrackedBreakout, leadership_pillar

LEADERSHIP = load_risk_config().regime.leadership
VCP, PULLBACK = Strategy.VCP_BREAKOUT, Strategy.LEADER_PULLBACK
T = date(2026, 10, 23)


def test_a_high_at_the_pivot_triggers_every_strategy_ungated() -> None:
    watched = [
        Watched("AAA", VCP, 50.0),
        Watched("AAA", PULLBACK, 49.0),
        Watched("BBB", VCP, 20.0),
        Watched("GONE", VCP, 10.0),
    ]
    bars = {"AAA": Bar(49.5, 50.0, 49.0, 49.8, 1e6), "BBB": Bar(19.0, 19.99, 18.5, 19.5, 1e6)}
    assert triggers(watched, bars, T) == [
        Breakout("AAA", VCP, 50.0, T, 49.8),
        Breakout("AAA", PULLBACK, 49.0, T, 49.8),
    ]


def test_a_gap_open_above_the_pivot_counts() -> None:
    [b] = triggers([Watched("AAA", VCP, 50.0)], {"AAA": Bar(56, 57, 55, 56.5, 1e6)}, T)
    assert b.pivot == 50.0


def test_the_result_is_a_close_above_the_pivot() -> None:
    b = Breakout("AAA", VCP, 50.0, T, 51.0)
    assert above_trigger(b, close_then_now=51.0, result_close=50.01)
    assert not above_trigger(b, close_then_now=51.0, result_close=50.0)


def test_the_result_rescales_the_pivot_after_a_split() -> None:
    b = Breakout("AAA", VCP, 50.0, T, 51.0)
    # A 2-for-1 split since: the trigger session's close now reads 25.50, the pivot 25.
    assert above_trigger(b, close_then_now=25.5, result_close=25.2)
    assert not above_trigger(b, close_then_now=25.5, result_close=24.9)


def sessions(n: int) -> list[date]:
    return [T - timedelta(days=n - 1 - i) for i in range(n)]


def test_for_pillar_counts_sessions_back_from_t() -> None:
    days = sessions(40)
    rows = [
        Tracked(days[-1], None),  # today: incomplete
        Tracked(days[-5], None),  # T-4: incomplete
        Tracked(days[-6], True),  # T-5: the window's first
        Tracked(days[-25], False),  # T-24: its last
        Tracked(days[-26], True),  # T-25: outside the rows passed on
    ]
    assert for_pillar(rows, days, LEADERSHIP) == [
        TrackedBreakout(0, None),
        TrackedBreakout(4, None),
        TrackedBreakout(5, True),
        TrackedBreakout(24, False),
    ]


def test_the_pillar_reads_only_complete_results() -> None:
    days = sessions(30)
    rows = [Tracked(days[-1 - ago], ago % 2 == 0) for ago in range(5, 25)]
    rows += [Tracked(days[-1 - ago], None) for ago in range(5)]
    leadership = leadership_pillar(for_pillar(rows, days, LEADERSHIP), LEADERSHIP)
    assert leadership.breakouts == 20
    assert leadership.share_above == pytest.approx(0.5)


def test_an_incomplete_result_in_the_window_is_an_error() -> None:
    days = sessions(30)
    with pytest.raises(ValueError, match="no complete result"):
        leadership_pillar(for_pillar([Tracked(days[-8], None)], days, LEADERSHIP), LEADERSHIP)
