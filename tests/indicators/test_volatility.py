import pytest

from auratrade.indicators.bars import Bar
from auratrade.indicators.volatility import adr_pct, atr, true_range

# A plain bar, a gap up and a gap down: true range includes the gaps, high - low doesn't.
BARS = [
    Bar(open=9, high=10, low=8, close=9, volume=0),
    Bar(open=10, high=11, low=9, close=10, volume=0),  # TR = 11 - 9 = 2
    Bar(open=14, high=15, low=13, close=14, volume=0),  # gap up: TR = 15 - 10 = 5
    Bar(open=8, high=9, low=7, close=8, volume=0),  # gap down: TR = 14 - 7 = 7
]


def test_true_range_includes_gaps() -> None:
    assert true_range(BARS) == [None, 2, 5, 7]
    assert true_range([]) == []


def test_atr_is_a_plain_average_of_true_range() -> None:
    assert atr(BARS, 2) == [None, None, 3.5, 6]
    assert atr(BARS, 3) == [None, None, None, pytest.approx(14 / 3)]
    assert atr([], 3) == []


def test_adr_pct() -> None:
    bars = [
        Bar(open=10, high=11, low=10, close=10, volume=0),  # 10%
        Bar(open=10, high=12, low=10, close=10, volume=0),  # 20%
        Bar(open=10, high=10, low=10, close=10, volume=0),  # 0%
    ]
    assert adr_pct(bars, 2) == [None, pytest.approx(0.15), pytest.approx(0.10)]
