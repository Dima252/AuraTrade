import pytest

from auratrade.indicators.series import (
    change,
    check_same_length,
    rolling_mean,
    rolling_sum,
)


def test_rolling_sum_and_mean() -> None:
    assert rolling_sum([1, 2, 3, 4, 5], 3) == [None, None, 6, 9, 12]
    assert rolling_mean([1, 2, 3, 4, 5], 2) == [None, 1.5, 2.5, 3.5, 4.5]
    assert rolling_sum([1, 2], 3) == [None, None]
    assert rolling_sum([], 3) == []


def test_change() -> None:
    result = change([100, 110, 121], 1)
    assert result[0] is None
    assert result[1:] == [pytest.approx(0.1), pytest.approx(0.1)]
    assert change([100, 110, 121], 2) == [None, None, pytest.approx(0.21)]


def test_validation() -> None:
    with pytest.raises(ValueError, match="window"):
        rolling_sum([1.0], 0)
    with pytest.raises(ValueError, match="lookback"):
        change([1.0], 0)
    with pytest.raises(ValueError, match="lengths differ"):
        check_same_length([1], [1, 2])
