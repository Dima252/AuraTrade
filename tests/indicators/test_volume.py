import math

import pytest

from auratrade.indicators.bars import Bar
from auratrade.indicators.volume import (
    PaceDay,
    build_pace_curve,
    normalize_session_volume,
    normalize_session_volumes,
    project_day_volume,
    up_down_volume_ratio,
)


def bars(closes: list[float], volumes: list[float]) -> list[Bar]:
    return [
        Bar(open=c, high=c, low=c, close=c, volume=v) for c, v in zip(closes, volumes, strict=True)
    ]


# Up days: t1 (200), t4 (300). Down day: t2 (50). t3 is unchanged and counts for neither.
SERIES = bars([10, 11, 10, 10, 12], [100, 200, 50, 70, 300])


def test_up_down_volume_ratio() -> None:
    assert up_down_volume_ratio(SERIES, 2) == [None, None, 4.0, 0.0, math.inf]
    assert up_down_volume_ratio(SERIES, 3) == [None, None, None, 4.0, 6.0]


def test_up_down_volume_ratio_without_up_or_down_days() -> None:
    assert up_down_volume_ratio(bars([10, 10, 10], [5, 5, 5]), 2) == [None, None, None]


def test_half_day_volume_normalization() -> None:
    assert normalize_session_volume(1000, 210, 390) == pytest.approx(1000 * 390 / 210)
    assert normalize_session_volume(1000, 390, 390) == 1000
    assert normalize_session_volumes([1000, 2100], [390, 210], 390) == [
        1000,
        pytest.approx(3900),
    ]
    with pytest.raises(ValueError, match="session_minutes"):
        normalize_session_volume(1000, 0, 390)


def test_pace_curve_is_the_median_cumulative_share() -> None:
    curve = build_pace_curve(
        [
            # Day volume 200 includes 100 outside the minute bars (auction, extended hours).
            PaceDay(minute_volumes=[10, 30, 60], day_volume=200),  # 0.05, 0.2, 0.5
            PaceDay(minute_volumes=[20, 20, 60], day_volume=100),  # 0.2, 0.4, 1.0
            PaceDay(minute_volumes=[0, 0, 0], day_volume=0),  # skipped
            PaceDay(minute_volumes=[10, 10, 80], day_volume=100),  # 0.1, 0.2, 1.0
        ]
    )
    assert curve == [pytest.approx(0.1), pytest.approx(0.2), pytest.approx(1.0)]


def test_pace_curve_validation() -> None:
    with pytest.raises(ValueError, match="no days"):
        build_pace_curve([PaceDay(minute_volumes=[0], day_volume=0)])
    with pytest.raises(ValueError, match="same"):
        build_pace_curve(
            [
                PaceDay(minute_volumes=[1], day_volume=1),
                PaceDay(minute_volumes=[1, 1], day_volume=2),
            ]
        )


def test_project_day_volume() -> None:
    curve = [0.1, 0.2, 1.0]
    assert project_day_volume(50, 2, curve, 3) == pytest.approx(250)
    assert project_day_volume(50, 5, curve, 3) == pytest.approx(50)  # clamped to the close
    assert project_day_volume(50, 0, curve, 3) is None


def test_project_day_volume_on_a_short_session_maps_onto_the_curve() -> None:
    # Half of a 2-minute session elapsed = position 2 of the 4-minute curve.
    assert project_day_volume(30, 1, [0.1, 0.3, 0.6, 1.0], 2) == pytest.approx(100)


def test_project_day_volume_edge_cases() -> None:
    assert project_day_volume(30, 1, [0.0, 1.0], 2) is None  # no volume yet on the curve
    with pytest.raises(ValueError, match="empty"):
        project_day_volume(30, 1, [], 2)
