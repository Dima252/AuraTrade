"""The monthly rebalance calendar (`calendar/rebalance.py`): R and E."""

from dataclasses import dataclass
from datetime import date
from typing import ClassVar

import pytest

from auratrade.calendar.rebalance import Rebalance, rebalances


@dataclass(frozen=True)
class Day:
    day: date
    is_early_close: bool = False


class TestRebalances:
    SESSIONS: ClassVar[list[Day]] = [
        Day(date(2024, 10, 30)),
        Day(date(2024, 10, 31)),
        Day(date(2024, 11, 1), is_early_close=True),  # a half day on the first session
        Day(date(2024, 11, 4)),
        Day(date(2024, 11, 29)),
        Day(date(2024, 12, 2)),
        Day(date(2024, 12, 3)),
        Day(date(2024, 12, 31)),
        Day(date(2025, 1, 2), is_early_close=True),  # no full session after it in range
    ]

    def test_first_session_of_each_month(self) -> None:
        assert rebalances(self.SESSIONS) == [
            Rebalance(date(2024, 10, 31), date(2024, 11, 4)),  # E moves, R doesn't
            Rebalance(date(2024, 11, 29), date(2024, 12, 2)),
        ]

    def test_offset(self) -> None:
        # The 2nd session: October's is in range (R = 10-30), November's is 11-04.
        assert rebalances(self.SESSIONS, offset=2) == [
            Rebalance(date(2024, 10, 30), date(2024, 10, 31)),
            Rebalance(date(2024, 11, 1), date(2024, 11, 4)),
            Rebalance(date(2024, 12, 2), date(2024, 12, 3)),
        ]

    def test_bad_offset(self) -> None:
        with pytest.raises(ValueError, match="offset"):
            rebalances(self.SESSIONS, offset=0)
