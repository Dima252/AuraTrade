from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from auratrade.config import OutlierConfig
from auratrade.ingest.bars import (
    BarField,
    Correction,
    IngestBar,
    apply_corrections,
    combine,
    coverage,
    find_outliers,
    sanity_problem,
    split_changed,
)

CFG = OutlierConfig(min_beyond_body=0.15, min_beyond_neighbours=0.15)
D1, D2, D3 = date(2024, 6, 7), date(2024, 6, 10), date(2024, 6, 11)


def bar(day: date, o: float, h: float, low: float, c: float, v: int = 1000) -> IngestBar:
    return IngestBar(day, o, h, low, c, v, c)


class TestSanity:
    def test_good_bar(self) -> None:
        assert sanity_problem(bar(D1, 10, 11, 9, 10.5)) is None

    @pytest.mark.parametrize(
        ("b", "why"),
        [
            (bar(D1, 10, 11, 9, 10.5, v=0), "zero volume"),
            (bar(D1, 10, 11, 0, 10.5), "non-positive price"),
            (replace(bar(D1, 10, 11, 9, 10.5), raw_close=0.0), "non-positive price"),
            (bar(D1, 10, 10.4, 9, 10.5), "high/low do not contain open and close"),
            (bar(D1, 10, 11, 10.2, 10.5), "high/low do not contain open and close"),
            (bar(D1, 10, 11, 11.5, 10.5), "high/low do not contain open and close"),
        ],
    )
    def test_rejects(self, b: IngestBar, why: str) -> None:
        assert sanity_problem(b) == why


def test_combine_attaches_raw_close_and_drops_unmatched() -> None:
    adjusted = [replace(bar(D1, 10, 11, 9, 10), raw_close=0.0), bar(D2, 10, 11, 9, 10)]
    out, missing = combine(adjusted, {D1: 100.0})
    assert [b.raw_close for b in out] == [100.0]
    assert missing == [D2]


class TestOutliers:
    def test_nvda_bad_high(self) -> None:
        # NVDA on its split day, 2024-06-10: a 195.95 print against a ~122 day.
        bars = [
            bar(D1, 119.77, 121.69, 118.02, 120.89),
            bar(D2, 120.37, 195.95, 117.01, 121.79),
            bar(D3, 121.77, 122.87, 118.74, 120.91),
        ]
        found = find_outliers(bars, CFG)
        assert found == [Correction(D2, BarField.HIGH, 195.95, 121.79)]
        fixed = apply_corrections(bars, found)
        assert fixed[1].high == 121.79
        assert fixed[0] == bars[0]
        assert fixed[2] == bars[2]

    def test_bad_low(self) -> None:
        bars = [bar(D1, 50, 51, 49, 50), bar(D2, 50, 51, 20, 50.5), bar(D3, 50, 52, 49.5, 51)]
        assert find_outliers(bars, CFG) == [Correction(D2, BarField.LOW, 20, 50)]
        assert apply_corrections(bars, find_outliers(bars, CFG))[1].low == 50

    def test_newest_bar_checked_against_one_neighbour(self) -> None:
        bars = [bar(D1, 50, 51, 49, 50), bar(D2, 50, 80, 49, 50.5)]
        assert [c.session for c in find_outliers(bars, CFG)] == [D2]

    def test_a_lone_bar_is_not_checked(self) -> None:
        assert find_outliers([bar(D1, 50, 90, 10, 50)], CFG) == []

    def test_a_real_gap_and_run_is_kept(self) -> None:
        # An episodic pivot: gap up 30%, a high well above the prior bar but near its own body.
        bars = [bar(D1, 50, 51, 49, 50), bar(D2, 65, 72, 64, 70), bar(D3, 70, 74, 69, 73)]
        assert find_outliers(bars, CFG) == []

    def test_spike_above_body_but_not_neighbours_is_kept(self) -> None:
        # Far above its own body, but the next session traded there too: a real reversal.
        bars = [bar(D1, 50, 51, 49, 50), bar(D2, 50, 60, 49, 50.5), bar(D3, 55, 62, 54, 60)]
        assert find_outliers(bars, CFG) == []


def test_split_changed() -> None:
    assert split_changed(100.0, 50.0, 0.02)
    assert split_changed(100.0, 1000.0, 0.02)
    assert not split_changed(100.0, 101.0, 0.02)


def test_coverage() -> None:
    assert coverage(set(), set()) == 1.0
    assert coverage({1, 2, 3, 4}, {1, 2, 3, 9}) == 0.75
