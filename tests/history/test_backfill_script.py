"""`scripts/backfill.py`: which session's bars are final."""

from __future__ import annotations

from datetime import UTC, date, datetime

from auratrade.calendar.sessions import MarketCalendar
from scripts import backfill
from tests.calendar_fixture import alpaca_times, to_session


def test_last_final_session() -> None:
    calendar = MarketCalendar(to_session(s) for s in alpaca_times())
    # 16:10 New York: the close plus the SIP delay hasn't passed yet.
    assert backfill.last_final_session(
        calendar, datetime(2026, 10, 15, 20, 10, tzinfo=UTC)
    ) == date(2026, 10, 14)
    assert backfill.last_final_session(
        calendar, datetime(2026, 10, 15, 20, 30, tzinfo=UTC)
    ) == date(2026, 10, 15)
    # A Saturday: the Friday.
    assert backfill.last_final_session(calendar, datetime(2026, 10, 17, 15, 0, tzinfo=UTC)) == date(
        2026, 10, 16
    )
