"""Calendar helpers on Alpaca's real 2025-12 to 2027-01 calendar (recorded fixture)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from auratrade.calendar.sessions import CalendarUnavailableError, MarketCalendar
from tests.calendar_fixture import alpaca_times, to_session

WED = date(2026, 10, 7)
SAT, SUN = date(2026, 10, 10), date(2026, 10, 11)
LABOR_DAY = date(2026, 9, 7)  # Monday holiday
GOOD_FRIDAY = date(2026, 4, 3)
THANKSGIVING = date(2026, 11, 26)
DAY_AFTER_THANKSGIVING = date(2026, 11, 27)  # early close, 13:00
JULY_3 = date(2026, 7, 3)  # Independence Day falls on a Saturday: observed Friday


@pytest.fixture(scope="module")
def cal() -> MarketCalendar:
    return MarketCalendar(to_session(s) for s in alpaca_times())


class TestSessionFor:
    def test_regular_session(self, cal: MarketCalendar) -> None:
        s = cal.session_for(WED)
        assert s is not None
        assert s.open_at == datetime(2026, 10, 7, 13, 30, tzinfo=UTC)  # 09:30 EDT
        assert s.close_at == datetime(2026, 10, 7, 20, 0, tzinfo=UTC)
        assert (s.minutes, s.is_early_close) == (390, False)
        assert not s.calendar_agrees  # not cross-checked yet

    @pytest.mark.parametrize("day", [SAT, SUN, LABOR_DAY, GOOD_FRIDAY, THANKSGIVING, JULY_3])
    def test_closed_days(self, cal: MarketCalendar, day: date) -> None:
        assert cal.session_for(day) is None
        assert not cal.is_session(day)

    def test_day_after_thanksgiving_is_an_early_close(self, cal: MarketCalendar) -> None:
        assert cal.is_session(DAY_AFTER_THANKSGIVING)
        assert cal.is_early_close(DAY_AFTER_THANKSGIVING)
        assert cal.session_minutes(DAY_AFTER_THANKSGIVING) == 210
        s = cal.session_for(DAY_AFTER_THANKSGIVING)
        assert s is not None
        assert s.close_at == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)  # 13:00 EST

    def test_times_follow_dst(self, cal: MarketCalendar) -> None:
        before, after = cal.session_for(date(2026, 3, 6)), cal.session_for(date(2026, 3, 9))
        assert before is not None
        assert after is not None
        assert before.open_at.hour == 14  # 09:30 EST
        assert after.open_at.hour == 13  # 09:30 EDT

    def test_year_has_251_sessions(self, cal: MarketCalendar) -> None:
        assert len(cal.sessions_between(date(2026, 1, 1), date(2026, 12, 31))) == 251

    def test_non_session_helpers_raise(self, cal: MarketCalendar) -> None:
        with pytest.raises(ValueError, match="not a session"):
            cal.is_early_close(GOOD_FRIDAY)
        with pytest.raises(ValueError, match="not a session"):
            cal.session_minutes(SAT)


class TestCounting:
    def test_previous_session_skips_weekends_and_holidays(self, cal: MarketCalendar) -> None:
        assert cal.previous_session(date(2026, 9, 8)).day == date(2026, 9, 4)  # over Labor Day
        assert cal.previous_session(date(2026, 4, 6)).day == date(2026, 4, 2)  # over Good Friday
        assert cal.previous_session(SUN).day == date(2026, 10, 9)
        assert cal.previous_session(WED).day == date(2026, 10, 6)

    def test_next_session(self, cal: MarketCalendar) -> None:
        assert cal.next_session(THANKSGIVING).day == DAY_AFTER_THANKSGIVING
        assert cal.next_session(date(2026, 4, 2)).day == date(2026, 4, 6)

    def test_sessions_between_is_inclusive(self, cal: MarketCalendar) -> None:
        days = [s.day for s in cal.sessions_between(date(2026, 4, 1), date(2026, 4, 7))]
        assert days == [date(2026, 4, d) for d in (1, 2, 6, 7)]
        assert cal.sessions_between(SAT, SUN) == []

    def test_n_sessions_back(self, cal: MarketCalendar) -> None:
        assert cal.n_sessions_back(WED, 0).day == WED
        assert cal.n_sessions_back(WED, 1).day == date(2026, 10, 6)
        assert cal.n_sessions_back(SUN, 0).day == date(2026, 10, 9)  # anchors on Friday
        # Apr 8 -> 7, 6, (Good Friday skipped) 2, 1, Mar 31.
        assert cal.n_sessions_back(date(2026, 4, 8), 5).day == date(2026, 3, 31)
        with pytest.raises(ValueError, match=">= 0"):
            cal.n_sessions_back(WED, -1)

    @pytest.mark.parametrize(
        ("day", "days", "long_weekend"),
        [
            (WED, 1, False),
            (date(2026, 10, 9), 3, False),  # Fri -> Mon
            (date(2026, 9, 4), 4, True),  # Fri before Labor Day -> Tue
            (date(2026, 4, 2), 4, True),  # Thu before Good Friday -> Mon
            (date(2026, 11, 25), 2, False),  # Wed before Thanksgiving -> Fri
            (SAT, 2, False),
        ],
    )
    def test_gap_to_next_open(
        self, cal: MarketCalendar, day: date, days: int, long_weekend: bool
    ) -> None:
        assert cal.days_until_next_open(day) == days


class TestCoverage:
    @pytest.mark.parametrize("day", [date(2025, 11, 28), date(2027, 2, 1)])
    def test_outside_the_synced_range_is_unknown_not_closed(
        self, cal: MarketCalendar, day: date
    ) -> None:
        with pytest.raises(CalendarUnavailableError, match="outside the synced calendar"):
            cal.is_session(day)

    def test_edges(self, cal: MarketCalendar) -> None:
        assert cal.first == date(2025, 12, 1)
        assert cal.last == date(2027, 1, 29)
        with pytest.raises(CalendarUnavailableError, match="no synced session before"):
            cal.previous_session(date(2025, 12, 1))
        with pytest.raises(CalendarUnavailableError, match="no synced session after"):
            cal.next_session(date(2027, 1, 29))
        with pytest.raises(CalendarUnavailableError, match="fewer than"):
            cal.n_sessions_back(date(2025, 12, 2), 5)

    def test_explicit_range_beyond_the_sessions(self) -> None:
        s = to_session(alpaca_times()[0])
        cal = MarketCalendar([s], first=date(2025, 11, 1), last=date(2025, 12, 31))
        assert not cal.is_session(date(2025, 12, 31))

    def test_empty_calendar_fails_closed(self) -> None:
        with pytest.raises(CalendarUnavailableError, match="empty"):
            MarketCalendar([]).is_session(WED)
