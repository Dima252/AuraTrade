"""Macro series: CBOE VIX/VIX3M (delayed quotes and history files) and FRED (spike 0.3d,
docs/spikes/macro.md; source order in README §5.1, #117).

- **Delayed-quote JSON** (`_VIX.json`): 15 minutes delayed. `last_trade_time` is New York
  wall-clock time with no zone; the final close shows `last_trade_time` 16:15:01 (the
  settlement) and was there by 16:32 ET. Choosing whether a quote is final is the caller's
  rule (ingest), not this client's.
- **History files** (`VIX_History.csv`): updated at about 18:30 to 21:30 ET. Judge freshness by
  the last row's date, never `Last-Modified`. The VIX file has rows on market holidays.
- **FRED** takes its key only as a query parameter, so its errors must never carry the URL
  (`VendorHttp` guarantees that). Missing observations are "." and are skipped.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from time import sleep as time_sleep
from zoneinfo import ZoneInfo

import httpx

from auratrade.data.http import RateLimiter, VendorError, VendorHttp

NY = ZoneInfo("America/New_York")
CBOE_QUOTE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_{symbol}.json"
CBOE_HISTORY_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{symbol}_History.csv"
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
# CBOE publishes no limit for its CDN; stay polite. FRED documents 120 requests a minute.
CBOE_PER_SECOND = 2
FRED_PER_MINUTE = 100


class CboeIndex(StrEnum):
    VIX = "VIX"
    VIX3M = "VIX3M"


class FredSeries(StrEnum):
    VIX = "VIXCLS"
    VIX3M = "VXVCLS"  # FRED still names VIX3M by its old ticker, VXV
    CREDIT_SPREAD = "BAMLH0A0HYM2"  # ICE BofA US high-yield OAS; LLM context only (#117)


@dataclass(frozen=True)
class DelayedQuote:
    index: CboeIndex
    current_price: float
    close: float
    prev_day_close: float
    last_trade_at: datetime  # UTC (converted from New York wall-clock time)


@dataclass(frozen=True)
class CboeHistory:
    index: CboeIndex
    closes: dict[date, float]


class CboeClient:
    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time_sleep,
    ) -> None:
        self._http = VendorHttp(
            "cboe",
            limiter=RateLimiter(per_second=CBOE_PER_SECOND, sleep=sleep),
            client=client,
            sleep=sleep,
        )

    def delayed_quote(self, index: CboeIndex) -> DelayedQuote:
        body = self._http.get_json(CBOE_QUOTE_URL.format(symbol=index.value))
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise VendorError("cboe", f"_{index.value}.json: no data", retryable=False)
        return DelayedQuote(
            index,
            float(data["current_price"]),
            float(data["close"]),
            float(data["prev_day_close"]),
            datetime.fromisoformat(data["last_trade_time"]).replace(tzinfo=NY).astimezone(UTC),
        )

    def history(self, index: CboeIndex) -> CboeHistory:
        text = self._http.get_text(CBOE_HISTORY_URL.format(symbol=index.value))
        return CboeHistory(index, parse_cboe_history(text))


def parse_cboe_history(text: str) -> dict[date, float]:
    """`DATE,OPEN,HIGH,LOW,CLOSE` rows with MM/DD/YYYY dates → close per date."""
    closes = {}
    for row in csv.DictReader(io.StringIO(text)):
        month, day, year = (int(part) for part in row["DATE"].split("/"))
        closes[date(year, month, day)] = float(row["CLOSE"])
    return closes


class FredClient:
    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time_sleep,
    ) -> None:
        self._key = api_key
        self._http = VendorHttp(
            "fred",
            limiter=RateLimiter(per_minute=FRED_PER_MINUTE, sleep=sleep),
            secrets=(api_key,),
            client=client,
            sleep=sleep,
        )

    def observations(self, series: FredSeries, start: date, end: date) -> dict[date, float]:
        body = self._http.get_json(
            FRED_URL,
            {
                "series_id": series.value,
                "observation_start": start.isoformat(),
                "observation_end": end.isoformat(),
                "api_key": self._key,
                "file_type": "json",
            },
        )
        if not isinstance(body, dict):
            raise VendorError("fred", "observations: unexpected response", retryable=False)
        return {
            date.fromisoformat(o["date"]): float(o["value"])
            for o in body.get("observations", [])
            if o["value"] not in (".", "")
        }
