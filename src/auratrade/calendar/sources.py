"""Session times from Alpaca (primary) and `pandas_market_calendars` (cross-check).

Alpaca's `open`/`close` are America/New_York wall-clock strings with no zone (spike 0.3c):
they are localized to New York for each date, then stored as UTC.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from time import sleep as time_sleep
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

NY = ZoneInfo("America/New_York")
REGULAR_CLOSE = time(16, 0)
# Paper only (CLAUDE.md §3.1); the calendar is the same on every account.
ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
TIMEOUT_S = 15.0


@dataclass(frozen=True)
class SessionTimes:
    day: date
    open_at: datetime  # UTC
    close_at: datetime  # UTC

    @property
    def is_early_close(self) -> bool:
        return self.close_at.astimezone(NY).time() < REGULAR_CLOSE

    @property
    def minutes(self) -> int:
        return int((self.close_at - self.open_at).total_seconds() // 60)


def _ny(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), tzinfo=NY).astimezone(UTC)


def parse_alpaca_calendar(rows: Iterable[Mapping[str, Any]]) -> list[SessionTimes]:
    sessions = []
    for row in rows:
        day = date.fromisoformat(row["date"])
        sessions.append(SessionTimes(day, _ny(day, row["open"]), _ny(day, row["close"])))
    return sorted(sessions, key=lambda s: s.day)


class AlpacaCalendarError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def _retryable(exc: BaseException) -> bool:
    return isinstance(exc, AlpacaCalendarError) and exc.retryable


class AlpacaCalendarClient:
    """`GET /v2/calendar`: read-only, free."""

    def __init__(
        self,
        key_id: str,
        secret_key: str,
        *,
        client: httpx.Client | None = None,
        base_url: str = ALPACA_PAPER_URL,
        sleep: Callable[[float], None] = time_sleep,
    ) -> None:
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}
        self._client = client or httpx.Client(timeout=TIMEOUT_S)
        self._url = f"{base_url}/v2/calendar"
        self._sleep = sleep

    def fetch(self, start: date, end: date) -> list[SessionTimes]:
        @retry(
            retry=retry_if_exception(_retryable),
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=1, max=10),
            sleep=self._sleep,
            reraise=True,
        )
        def attempt() -> list[SessionTimes]:
            return parse_alpaca_calendar(self._get(start, end))

        return attempt()

    def _get(self, start: date, end: date) -> list[Mapping[str, Any]]:
        try:
            response = self._client.get(
                self._url,
                params={"start": start.isoformat(), "end": end.isoformat()},
                headers=self._headers,
            )
        except httpx.TransportError as exc:
            raise AlpacaCalendarError(
                f"transport error: {type(exc).__name__}", retryable=True
            ) from None
        if response.status_code != httpx.codes.OK:
            retryable = response.status_code == httpx.codes.TOO_MANY_REQUESTS or (
                response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR
            )
            raise AlpacaCalendarError(f"HTTP {response.status_code}", retryable=retryable)
        body = response.json()
        if not isinstance(body, list):
            raise AlpacaCalendarError("unexpected calendar response", retryable=False)
        return body


def exchange_calendar_sessions(start: date, end: date, name: str = "NYSE") -> list[SessionTimes]:
    """The cross-check: `pandas_market_calendars` (keep it updated, spike 0.3c)."""
    import pandas_market_calendars as mcal

    schedule = mcal.get_calendar(name).schedule(start_date=start, end_date=end)
    return [
        SessionTimes(
            day=index.date(),
            open_at=row.market_open.to_pydatetime().astimezone(UTC),
            close_at=row.market_close.to_pydatetime().astimezone(UTC),
        )
        for index, row in schedule.iterrows()
    ]
