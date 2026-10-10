import pytest

from auratrade.indicators.breadth import net_new_highs_average


def test_net_new_highs_average() -> None:
    assert net_new_highs_average([5, 3, 0], [1, 4, 2], 2) == [None, 1.5, -1.5]
    with pytest.raises(ValueError, match="lengths differ"):
        net_new_highs_average([1], [], 2)
