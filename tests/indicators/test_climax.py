from typing import Any

import pytest

from auratrade.indicators.bars import Bar
from auratrade.indicators.climax import ClimaxSignals, climax_signals

# Every signal switched off; each test switches on the one it checks.
OFF: dict[str, Any] = {
    "long_average": None,
    "exhaustion_min_gap": 10.0,
    "up_day_clusters": [],
    "min_above_long_average": 0.70,
    "fast_gain_min": 10.0,
    "fast_gain_window": 1,
    "fast_gain_min_move_sessions": 0,
}
NONE = ClimaxSignals(
    largest_gain=False,
    exhaustion_gap=False,
    heaviest_volume_up_day=False,
    up_day_cluster=False,
    far_above_long_average=False,
    fast_gain=False,
)


def bars(closes: list[float], volumes: list[float] | None = None) -> list[Bar]:
    volumes = volumes or [1000] * len(closes)
    return [
        Bar(open=c, high=c, low=c, close=c, volume=v) for c, v in zip(closes, volumes, strict=True)
    ]


def signals(series: list[Bar], at: int, move_start: int, **overrides: Any) -> ClimaxSignals:
    return climax_signals(series, at=at, move_start=move_start, **{**OFF, **overrides})


def test_largest_gain_since_the_move_began() -> None:
    # Gains: +5% (breakout, session 1), +0.95%, +3.77%, +6.36%.
    series = bars([100, 105, 106, 110, 117])
    assert signals(series, 4, 1).largest_gain
    assert not signals(series, 3, 1).largest_gain
    assert not signals(series, 1, 1).largest_gain  # the breakout day doesn't count against itself
    falling = bars([100, 99, 98.5])
    assert not signals(falling, 2, 1).largest_gain  # a smaller loss is not a gain


def test_exhaustion_gap() -> None:
    series = [
        Bar(open=100, high=110, low=99, close=108, volume=1000),
        Bar(open=112, high=115, low=111, close=114, volume=1000),
    ]
    assert signals(series, 1, 0, exhaustion_min_gap=0.01).exhaustion_gap  # 112 > 110 * 1.01
    assert not signals(series, 1, 0, exhaustion_min_gap=0.02).exhaustion_gap  # 112 < 112.2


def test_heaviest_volume_up_day_since_the_breakout() -> None:
    series = bars([100, 105, 104, 106], [5000, 1000, 800, 1200])
    # Session 0 (before the breakout) had more volume but doesn't count.
    assert signals(series, 3, 1).heaviest_volume_up_day
    down = bars([100, 105, 104, 103], [5000, 1000, 800, 1200])
    assert not signals(down, 3, 1).heaviest_volume_up_day
    equal = bars([100, 105, 104, 106], [5000, 1000, 800, 1000])
    assert not signals(equal, 3, 1).heaviest_volume_up_day


def test_up_day_clusters() -> None:
    # Sessions 1-8: seven up closes and one down (session 3).
    series = bars([10, 11, 12, 11.5, 13, 14, 15, 16, 17])
    assert signals(series, 8, 0, up_day_clusters=[(7, 8)]).up_day_cluster
    assert not signals(series, 8, 0, up_day_clusters=[(8, 8)]).up_day_cluster
    assert signals(series, 8, 0, up_day_clusters=[(8, 8), (7, 8)]).up_day_cluster
    assert not signals(series, 6, 0, up_day_clusters=[(7, 8)]).up_day_cluster  # too little history


def test_far_above_the_200_day() -> None:
    series = bars([100, 171])
    assert signals(series, 1, 0, long_average=100.0).far_above_long_average
    assert not signals(bars([100, 169]), 1, 0, long_average=100.0).far_above_long_average


def test_fast_gain_only_after_an_extended_run() -> None:
    # Lowest close of the 15 sessions before session 20 is 100; session 20 closes 26% above it.
    series = bars([100.0] * 20 + [126.0])
    rules = {"fast_gain_min": 0.25, "fast_gain_window": 15}
    assert signals(series, 20, 0, **rules, fast_gain_min_move_sessions=20).fast_gain
    assert not signals(series, 20, 0, **rules, fast_gain_min_move_sessions=21).fast_gain
    assert not signals(bars([100.0] * 20 + [124.0]), 20, 0, **rules).fast_gain


def test_count_and_nothing_firing() -> None:
    assert signals(bars([100, 100]), 1, 0) == NONE
    assert NONE.count == 0
    series = bars([100, 105, 104, 110], [1000, 1000, 800, 2000])
    fired = signals(series, 3, 1, long_average=60.0)  # largest gain, heaviest up volume, 83% above
    assert fired.count == 3


def test_index_validation() -> None:
    with pytest.raises(IndexError):
        signals(bars([100, 101]), 0, 1)
    with pytest.raises(IndexError):
        signals(bars([100, 101]), 2, 0)
