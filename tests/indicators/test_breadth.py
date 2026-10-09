import pytest

from auratrade.indicators.breadth import (
    fraction_above,
    net_new_highs_average,
    new_highs,
    new_lows,
)


def test_new_highs_and_lows_need_a_full_window() -> None:
    assert new_highs([1, 3, 2, 3, 4], 3) == [False, False, False, True, True]
    assert new_lows([5, 3, 4, 3, 2], 3) == [False, False, False, True, True]


def test_fraction_above() -> None:
    # Equal to the average isn't above it; a symbol with no average yet isn't counted.
    assert fraction_above([(10, 9), (10, 11), (10, None), (10, 10)]) == pytest.approx(1 / 3)
    assert fraction_above([(10, None)]) is None
    assert fraction_above([]) is None


def test_net_new_highs_average() -> None:
    assert net_new_highs_average([5, 3, 0], [1, 4, 2], 2) == [None, 1.5, -1.5]
    with pytest.raises(ValueError, match="lengths differ"):
        net_new_highs_average([1], [], 2)
