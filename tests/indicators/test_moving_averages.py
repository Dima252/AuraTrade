from auratrade.indicators.moving_averages import ema, falling, rising, sma


def test_sma() -> None:
    assert sma([1, 2, 3, 4, 5], 2) == [None, 1.5, 2.5, 3.5, 4.5]


def test_ema_is_seeded_with_the_sma() -> None:
    # alpha = 2 / (3 + 1) = 0.5; seed = mean(1, 2, 3) = 2;
    # then 2 + 0.5 * (4 - 2) = 3 and 3 + 0.5 * (5 - 3) = 4.
    assert ema([1, 2, 3, 4, 5], 3) == [None, None, 2, 3, 4]


def test_ema_short_history() -> None:
    assert ema([1, 2], 3) == [None, None]
    assert ema([], 3) == []


def test_ema_window_one_is_the_value() -> None:
    assert ema([4, 7, 1], 1) == [4, 7, 1]


def test_rising_and_falling() -> None:
    assert rising([1, 2, None, 4, 3], 2) == [False, False, False, True, False]
    assert falling([5, 4, 3, 4], 2) == [False, False, True, False]
    # Flat is neither.
    assert rising([2, 2], 1) == [False, False]
    assert falling([2, 2], 1) == [False, False]
