"""Finnhub free tier: the upcoming earnings calendar, the last 4 surprises, and the company
profile as the industry fallback (spike 0.3e, docs/spikes/fundamentals.md).

- The key goes in the `X-Finnhub-Token` header, never the query string.
- 60 requests a minute (and 30 a second).
- The calendar returns at most 1,500 rows a request, so it is queried one day at a time,
  and a full page is an error rather than a silently truncated day. It has no past dates.
- `hour` is blank for most reports; it maps to `UNKNOWN`, and ingest fills it from the
  company's 8-K habit (#119).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from time import sleep as time_sleep
from typing import Any, Literal

import httpx

from auratrade.data.http import RateLimiter, VendorError, VendorHttp

BASE_URL = "https://finnhub.io/api/v1"
PER_MINUTE = 55
PER_SECOND = 25
CALENDAR_ROW_CAP = 1500

# The `earnings_events.timing` values.
EarningsTiming = Literal["BEFORE_OPEN", "DURING_MARKET", "AFTER_CLOSE", "UNKNOWN"]
_TIMING: dict[str, EarningsTiming] = {
    "bmo": "BEFORE_OPEN",
    "dmh": "DURING_MARKET",
    "amc": "AFTER_CLOSE",
}


@dataclass(frozen=True)
class CalendarEntry:
    symbol: str
    report_date: date
    timing: EarningsTiming
    fiscal_year: int | None
    fiscal_quarter: int | None
    eps_estimate: float | None
    eps_actual: float | None
    revenue_estimate: int | None
    revenue_actual: int | None


@dataclass(frozen=True)
class Surprise:
    period: date  # fiscal quarter end, as Finnhub dates it
    fiscal_year: int | None
    fiscal_quarter: int | None
    actual: float | None  # Finnhub's adjusted EPS, not GAAP
    estimate: float | None
    surprise_pct: float | None


@dataclass(frozen=True)
class Profile:
    symbol: str
    name: str
    industry: str | None
    exchange: str | None
    country: str | None
    ipo: date | None


class FinnhubClient:
    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time_sleep,
    ) -> None:
        self._http = VendorHttp(
            "finnhub",
            limiter=RateLimiter(per_second=PER_SECOND, per_minute=PER_MINUTE, sleep=sleep),
            headers={"X-Finnhub-Token": api_key},
            secrets=(api_key,),
            client=client,
            sleep=sleep,
        )

    def earnings_calendar(self, day: date) -> list[CalendarEntry]:
        body = self._http.get_json(
            f"{BASE_URL}/calendar/earnings", {"from": day.isoformat(), "to": day.isoformat()}
        )
        rows = body.get("earningsCalendar") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise VendorError("finnhub", "/calendar/earnings: unexpected response", retryable=False)
        if len(rows) >= CALENDAR_ROW_CAP:
            raise VendorError(
                "finnhub",
                f"/calendar/earnings: {day} hit the {CALENDAR_ROW_CAP}-row cap (incomplete day)",
                retryable=False,
            )
        return [
            CalendarEntry(
                r["symbol"],
                date.fromisoformat(r["date"]),
                _TIMING.get(r.get("hour") or "", "UNKNOWN"),
                r.get("year"),
                r.get("quarter"),
                _float(r.get("epsEstimate")),
                _float(r.get("epsActual")),
                _int(r.get("revenueEstimate")),
                _int(r.get("revenueActual")),
            )
            for r in rows
        ]

    def earnings_surprises(self, symbol: str) -> list[Surprise]:
        body = self._http.get_json(f"{BASE_URL}/stock/earnings", {"symbol": symbol})
        if not isinstance(body, list):
            raise VendorError("finnhub", "/stock/earnings: unexpected response", retryable=False)
        return [
            Surprise(
                date.fromisoformat(r["period"]),
                r.get("year"),
                r.get("quarter"),
                _float(r.get("actual")),
                _float(r.get("estimate")),
                _float(r.get("surprisePercent")),
            )
            for r in body
        ]

    def profile(self, symbol: str) -> Profile | None:
        """None for a symbol Finnhub doesn't know (it answers `{}`)."""
        body = self._http.get_json(f"{BASE_URL}/stock/profile2", {"symbol": symbol})
        if not isinstance(body, dict):
            raise VendorError("finnhub", "/stock/profile2: unexpected response", retryable=False)
        if not body:
            return None
        ipo = body.get("ipo")
        return Profile(
            body.get("ticker") or symbol,
            body.get("name") or "",
            body.get("finnhubIndustry") or None,
            body.get("exchange") or None,
            body.get("country") or None,
            date.fromisoformat(ipo) if ipo else None,
        )


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def _int(value: Any) -> int | None:
    return None if value is None else round(float(value))
