import pytest
from hypothesis import given
from hypothesis import strategies as st

from auratrade.indicators.series import (
    change,
    check_same_length,
    rolling_max,
    rolling_mean,
    rolling_min,
    rolling_sum,
)


def test_rolling_sum_and_mean() -> None:
    assert rolling_sum([1, 2, 3, 4, 5], 3) == [None, None, 6, 9, 12]
    assert rolling_mean([1, 2, 3, 4, 5], 2) == [None, 1.5, 2.5, 3.5, 4.5]
    assert rolling_sum([1, 2], 3) == [None, None]
    assert rolling_sum([], 3) == []


def test_rolling_extremes() -> None:
    values = [3, 1, 4, 1, 5, 9, 2, 6]
    assert rolling_max(values, 3) == [None, None, 4, 4, 5, 9, 9, 9]
    assert rolling_min(values, 3) == [None, None, 1, 1, 1, 1, 2, 2]
    assert rolling_max(values, 1) == values


@given(st.lists(st.floats(min_value=-1e6, max_value=1e6), max_size=60), st.integers(1, 10))
def test_rolling_extremes_match_naive(values: list[float], window: int) -> None:
    windows = [values[t - window + 1 : t + 1] for t in range(window - 1, len(values))]
    warm_up: list[float | None] = [None] * min(window - 1, len(values))
    assert rolling_max(values, window) == warm_up + [max(w) for w in windows]
    assert rolling_min(values, window) == warm_up + [min(w) for w in windows]


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
