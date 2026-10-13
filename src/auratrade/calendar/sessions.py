"""Trading sessions and the session-counting helpers (README §4).

Every market window counts sessions, never calendar days. A date outside the synced range
is *unknown*, not closed: the helpers raise `CalendarUnavailableError`, so jobs fail closed
and alert instead of quietly skipping as "market closed".
"""

from __future__ import annotations

import bisect
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Connection, Engine, orm, select

from auratrade.db.models import MarketSession

# Fri → Mon is 3 days; anything longer is a long weekend (README §4, "Long weekends").
NORMAL_WEEKEND_DAYS = 3


class CalendarUnavailableError(RuntimeError):
    """The calendar can't answer for this date (outside the synced range, or empty)."""


@dataclass(frozen=True)
class Session:
    day: date
    open_at: datetime  # UTC
    close_at: datetime  # UTC
    is_early_close: bool
    minutes: int
    data_status: str = "PENDING"
    calendar_check: str = "UNCHECKED"

    @property
    def calendar_agrees(self) -> bool:
        """False on a disagreement, or before the cross-check ran: no new entries (§4)."""
        return self.calendar_check == "AGREE"


class MarketCalendar:
    """An in-memory calendar over a synced range. The range defaults to the first and last
    session; a gap inside it is a closed day, a date outside it is unknown."""

    def __init__(
        self, sessions: Iterable[Session], *, first: date | None = None, last: date | None = None
    ) -> None:
        self._by_day = {s.day: s for s in sessions}
        self._days = sorted(self._by_day)
        self.first = first if first is not None else (self._days[0] if self._days else None)
        self.last = last if last is not None else (self._days[-1] if self._days else None)

    def _check(self, day: date) -> None:
        if self.first is None or self.last is None:
            raise CalendarUnavailableError("market_sessions is empty: sync the calendar")
        if not self.first <= day <= self.last:
            raise CalendarUnavailableError(
                f"{day} is outside the synced calendar ({self.first} to {self.last})"
            )

    def session_for(self, day: date) -> Session | None:
        """The session on `day`, or None if the market is closed that day."""
        self._check(day)
        return self._by_day.get(day)

    def is_session(self, day: date) -> bool:
        return self.session_for(day) is not None

    def _session(self, day: date) -> Session:
        session = self.session_for(day)
        if session is None:
            raise ValueError(f"{day} is not a session")
        return session

    def is_early_close(self, day: date) -> bool:
        return self._session(day).is_early_close

    def session_minutes(self, day: date) -> int:
        return self._session(day).minutes

    def previous_session(self, day: date) -> Session:
        """The last session strictly before `day`."""
        self._check(day)
        i = bisect.bisect_left(self._days, day) - 1
        if i < 0:
            raise CalendarUnavailableError(f"no synced session before {day}")
        return self._by_day[self._days[i]]

    def next_session(self, day: date) -> Session:
        """The first session strictly after `day`."""
        self._check(day)
        i = bisect.bisect_right(self._days, day)
        if i >= len(self._days):
            raise CalendarUnavailableError(f"no synced session after {day}")
        return self._by_day[self._days[i]]

    def sessions_between(self, start: date, end: date) -> list[Session]:
        """Sessions from `start` to `end`, both inclusive."""
        self._check(start)
        self._check(end)
        lo = bisect.bisect_left(self._days, start)
        hi = bisect.bisect_right(self._days, end)
        return [self._by_day[d] for d in self._days[lo:hi]]

    def n_sessions_back(self, day: date, n: int) -> Session:
        """Count `n` sessions back from the latest session on or before `day`
        (`n=0` is that session itself; `n=1` the one before it)."""
        if n < 0:
            raise ValueError("n must be >= 0")
        self._check(day)
        i = bisect.bisect_right(self._days, day) - 1 - n
        if i < 0:
            raise CalendarUnavailableError(f"fewer than {n + 1} synced sessions up to {day}")
        return self._by_day[self._days[i]]

    def days_until_next_open(self, day: date) -> int:
        """Calendar days from `day` to the next session (Fri → Mon = 3). This measures the
        gap a position is held through, not a market window."""
        return (self.next_session(day).day - day).days

    def is_long_weekend(self, day: date) -> bool:
        return self.days_until_next_open(day) > NORMAL_WEEKEND_DAYS


def load_calendar(conn: Connection) -> MarketCalendar:
    rows = orm.Session(conn).scalars(select(MarketSession)).all()
    return MarketCalendar(
        Session(
            day=r.session_date,
            open_at=r.open_at,
            close_at=r.close_at,
            is_early_close=r.is_early_close,
            minutes=r.minutes,
            data_status=r.data_status,
            calendar_check=r.calendar_check,
        )
        for r in rows
    )


class DbCalendar:
    """`market_sessions`, loaded on first use (inside the job runner's error handling)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._calendar: MarketCalendar | None = None
        self._lock = threading.Lock()

    @property
    def calendar(self) -> MarketCalendar:
        with self._lock:
            if self._calendar is None:
                with self._engine.connect() as conn:
                    self._calendar = load_calendar(conn)
            return self._calendar

    def is_session(self, day: date) -> bool:
        return self.calendar.is_session(day)
