import pytest

from auratrade.indicators.bars import Bar
from auratrade.indicators.volume import (
    normalize_session_volume,
    normalize_session_volumes,
)


def bars(closes: list[float], volumes: list[float]) -> list[Bar]:
    return [
        Bar(open=c, high=c, low=c, close=c, volume=v) for c, v in zip(closes, volumes, strict=True)
    ]


# Up days: t1 (200), t4 (300). Down day: t2 (50). t3 is unchanged and counts for neither.
SERIES = bars([10, 11, 10, 10, 12], [100, 200, 50, 70, 300])


def test_half_day_volume_normalization() -> None:
    assert normalize_session_volume(1000, 210, 390) == pytest.approx(1000 * 390 / 210)
    assert normalize_session_volume(1000, 390, 390) == 1000
    assert normalize_session_volumes([1000, 2100], [390, 210], 390) == [
        1000,
        pytest.approx(3900),
    ]
    with pytest.raises(ValueError, match="session_minutes"):
        normalize_session_volume(1000, 0, 390)
