from auratrade.indicators.bars import Bar
from auratrade.indicators.ranges import highest_high, lowest_low


def bar(high: float, low: float) -> Bar:
    return Bar(open=low, high=high, low=low, close=low, volume=0)


def test_highest_high_and_lowest_low_count_sessions() -> None:
    bars = [bar(10, 8), bar(12, 9), bar(11, 7), bar(9, 8), bar(9.5, 8.5)]
    assert highest_high(bars, 3) == [None, None, 12, 12, 11]
    assert lowest_low(bars, 3) == [None, None, 7, 7, 7]
    assert highest_high(bars, 1) == [10, 12, 11, 9, 9.5]
