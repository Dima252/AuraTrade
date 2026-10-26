"""Alpaca market data on the free plan: SIP and IEX bars, IEX latest trades and quotes,
the corporate-actions feed, and the asset list (spike 0.3a, docs/spikes/alpaca-data.md).

- **Recent SIP is refused** (`403 subscription does not permit querying recent SIP data`)
  for anything newer than 15 minutes, and a date-only `end` for today counts as recent.
  SIP requests therefore send an explicit `end`, clipped to now minus 16 minutes.
- **Daily bars** are stamped at midnight New York time; `DailyBar.session` is that date.
- **Corporate actions** filter on `process_date`, not `ex_date`: a dividend going ex on
  10-15 has process date 10-16, and announced splits are listed ahead with a process date
  on or a day before the ex-date (checked 2026-10-15). To find events by ex-date, query a
  padded window and filter on `CorporateAction.effective_date`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from time import sleep as time_sleep
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from auratrade.data.http import Params, RateLimiter, VendorError, VendorHttp

NY = ZoneInfo("America/New_York")
DATA_URL = "https://data.alpaca.markets"
# Paper only until Phase 8 (CLAUDE.md §3.1); the asset list is the same on every account.
TRADING_URL = "https://paper-api.alpaca.markets"
# 200 requests a minute on the free plan, shared by every data endpoint (spike 0.3a).
REQUESTS_PER_MINUTE = 190
# Tested with 1,000 symbols a request; a page holds at most 10,000 bars.
MAX_SYMBOLS = 1000
PAGE_LIMIT = 10_000
CORPORATE_ACTIONS_PAGE_LIMIT = 1000
SIP_DELAY = timedelta(minutes=16)
# After the extended session, so a daily request for `end` always covers that day's bar.
DAY_END = time(20, 0)


class Feed(StrEnum):
    SIP = "sip"
    IEX = "iex"


class Adjustment(StrEnum):
    RAW = "raw"
    SPLIT = "split"
    ALL = "all"
    """Splits and dividends: total return (the portfolio's data checks only, never a signal)."""


class CorporateActionKind(StrEnum):
    SPLIT = "SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    CASH_MERGER = "CASH_MERGER"
    STOCK_MERGER = "STOCK_MERGER"
    SPIN_OFF = "SPIN_OFF"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"
    CASH_DIVIDEND = "CASH_DIVIDEND"


# Request type → (response key, kind, field holding the affected symbol).
_ACTION_TYPES: dict[str, tuple[str, CorporateActionKind, str]] = {
    "forward_split": ("forward_splits", CorporateActionKind.SPLIT, "symbol"),
    "reverse_split": ("reverse_splits", CorporateActionKind.REVERSE_SPLIT, "symbol"),
    "cash_merger": ("cash_mergers", CorporateActionKind.CASH_MERGER, "acquiree_symbol"),
    "stock_merger": ("stock_mergers", CorporateActionKind.STOCK_MERGER, "acquiree_symbol"),
    "spin_off": ("spin_offs", CorporateActionKind.SPIN_OFF, "source_symbol"),
    "name_change": ("name_changes", CorporateActionKind.SYMBOL_CHANGE, "old_symbol"),
    "cash_dividend": ("cash_dividends", CorporateActionKind.CASH_DIVIDEND, "symbol"),
}
_BY_KIND = {kind: request for request, (_, kind, _) in _ACTION_TYPES.items()}
_BY_KEY = {key: (kind, field) for key, kind, field in _ACTION_TYPES.values()}


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    session: date
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class MinuteBar:
    symbol: str
    start: datetime  # UTC, the minute's first instant
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class Trade:
    symbol: str
    price: float
    size: int
    at: datetime  # UTC


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    bid_size: int
    ask_size: int
    at: datetime  # UTC


@dataclass(frozen=True)
class CorporateAction:
    kind: CorporateActionKind
    symbol: str
    # The ex-date; mergers have an effective date and name changes only a process date.
    effective_date: date
    process_date: date | None
    # New shares per old share for splits (3.0 = 3-for-1, 0.1 = 1-for-10 reverse).
    ratio: float | None
    source_id: str
    terms: dict[str, Any]


@dataclass(frozen=True)
class Asset:
    id: str
    symbol: str
    name: str
    exchange: str
    status: str
    tradable: bool
    attributes: tuple[str, ...]


def alpaca_http(
    key_id: str,
    secret_key: str,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time_sleep,
    limiter: RateLimiter | None = None,
) -> VendorHttp:
    """One per account: the market-data, news and asset clients share its rate limit."""
    return VendorHttp(
        "alpaca",
        limiter=limiter or RateLimiter(per_minute=REQUESTS_PER_MINUTE, sleep=sleep),
        headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key},
        secrets=(key_id, secret_key),
        client=client,
        sleep=sleep,
    )


def parse_timestamp(value: str) -> datetime:
    # RFC 3339 with up to nanoseconds; Python keeps microseconds.
    return datetime.fromisoformat(value).astimezone(UTC)


def rfc3339(at: datetime) -> str:
    if at.tzinfo is None:
        raise ValueError("timezone-aware datetimes only")
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _chunks(symbols: Iterable[str], size: int = MAX_SYMBOLS) -> Iterator[list[str]]:
    unique = sorted(set(symbols))
    for i in range(0, len(unique), size):
        yield unique[i : i + size]


class AlpacaDataClient:
    """`workers` > 1 fetches the symbol chunks of a bars request concurrently: the requests are
    bound by the server's response time (3 to 15 s for 1,000 symbols, measured), not by the
    rate limit, which the shared limiter still enforces."""

    def __init__(
        self,
        http: VendorHttp,
        *,
        now: Callable[[], datetime] | None = None,
        workers: int = 1,
    ) -> None:
        self._http = http
        self._now = now or (lambda: datetime.now(UTC))
        self._workers = workers

    def daily_bars(
        self,
        symbols: Iterable[str],
        start: date,
        end: date,
        *,
        adjustment: Adjustment = Adjustment.SPLIT,
    ) -> dict[str, list[DailyBar]]:
        """SIP daily bars per symbol, oldest first. Symbols without bars are absent."""
        first = datetime.combine(start, time(0), tzinfo=NY)
        last = self._sip_end(datetime.combine(end, DAY_END, tzinfo=NY))
        out: dict[str, list[DailyBar]] = {}
        if last < first:
            return out
        for symbol, raw in self._bars(symbols, "1Day", first, last, Feed.SIP, adjustment):
            out.setdefault(symbol, []).append(
                DailyBar(
                    symbol,
                    parse_timestamp(raw["t"]).astimezone(NY).date(),
                    float(raw["o"]),
                    float(raw["h"]),
                    float(raw["l"]),
                    float(raw["c"]),
                    int(raw["v"]),
                )
            )
        return out

    def minute_bars(
        self,
        symbols: Iterable[str],
        start: datetime,
        end: datetime,
        *,
        feed: Feed,
        adjustment: Adjustment = Adjustment.RAW,
    ) -> dict[str, list[MinuteBar]]:
        """1-minute bars in [start, end], oldest first. IEX history starts 2020-07-27."""
        last = self._sip_end(end) if feed is Feed.SIP else end
        out: dict[str, list[MinuteBar]] = {}
        if last < start:
            return out
        for symbol, raw in self._bars(symbols, "1Min", start, last, feed, adjustment):
            out.setdefault(symbol, []).append(
                MinuteBar(
                    symbol,
                    parse_timestamp(raw["t"]),
                    float(raw["o"]),
                    float(raw["h"]),
                    float(raw["l"]),
                    float(raw["c"]),
                    int(raw["v"]),
                )
            )
        return out

    def latest_trades(self, symbols: Iterable[str], *, feed: Feed = Feed.IEX) -> dict[str, Trade]:
        out = {}
        for chunk in _chunks(symbols):
            body = self._http.get_json(
                f"{DATA_URL}/v2/stocks/trades/latest",
                {"symbols": ",".join(chunk), "feed": feed.value},
            )
            for symbol, raw in body.get("trades", {}).items():
                at = parse_timestamp(raw["t"])
                out[symbol] = Trade(symbol, float(raw["p"]), int(raw["s"]), at)
        return out

    def latest_quotes(self, symbols: Iterable[str], *, feed: Feed = Feed.IEX) -> dict[str, Quote]:
        out = {}
        for chunk in _chunks(symbols):
            body = self._http.get_json(
                f"{DATA_URL}/v2/stocks/quotes/latest",
                {"symbols": ",".join(chunk), "feed": feed.value},
            )
            for symbol, raw in body.get("quotes", {}).items():
                out[symbol] = Quote(
                    symbol,
                    float(raw["bp"]),
                    float(raw["ap"]),
                    int(raw["bs"]),
                    int(raw["as"]),
                    parse_timestamp(raw["t"]),
                )
        return out

    def corporate_actions(
        self,
        start: date,
        end: date,
        *,
        symbols: Iterable[str] | None = None,
        kinds: Iterable[CorporateActionKind] = tuple(CorporateActionKind),
    ) -> list[CorporateAction]:
        """Actions whose **process date** is in [start, end] (module docstring)."""
        types = ",".join(sorted(_BY_KIND[k] for k in set(kinds)))
        chunks: Iterable[list[str] | None] = [None] if symbols is None else _chunks(symbols)
        out: list[CorporateAction] = []
        for chunk in chunks:
            params: dict[str, str | int | None] = {
                "types": types,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": CORPORATE_ACTIONS_PAGE_LIMIT,
                "symbols": ",".join(chunk) if chunk is not None else None,
            }
            for body in self._pages(f"{DATA_URL}/v1/corporate-actions", params):
                for key, items in body.get("corporate_actions", {}).items():
                    if key in _BY_KEY:
                        out.extend(_corporate_action(key, item) for item in items)
        return out

    def assets(self, *, status: str | None = "active") -> list[Asset]:
        """US equities from the trading API's asset list (no ETF or type flag, #111)."""
        body = self._http.get_json(
            f"{TRADING_URL}/v2/assets", {"status": status, "asset_class": "us_equity"}
        )
        if not isinstance(body, list):
            raise VendorError("alpaca", "/v2/assets: unexpected response", retryable=False)
        return [
            Asset(
                str(a["id"]),
                a["symbol"],
                a.get("name") or "",
                a["exchange"],
                a["status"],
                bool(a["tradable"]),
                tuple(a.get("attributes") or ()),
            )
            for a in body
        ]

    def _sip_end(self, end: datetime) -> datetime:
        return min(end, self._now() - SIP_DELAY)

    def _bars(
        self,
        symbols: Iterable[str],
        timeframe: str,
        start: datetime,
        end: datetime,
        feed: Feed,
        adjustment: Adjustment,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        def chunk_bars(chunk: list[str]) -> list[tuple[str, dict[str, Any]]]:
            params = {
                "symbols": ",".join(chunk),
                "timeframe": timeframe,
                "start": rfc3339(start),
                "end": rfc3339(end),
                "feed": feed.value,
                "adjustment": adjustment.value,
                "limit": PAGE_LIMIT,
            }
            return [
                (symbol, raw)
                for body in self._pages(f"{DATA_URL}/v2/stocks/bars", params)
                for symbol, bars in (body.get("bars") or {}).items()
                for raw in bars
            ]

        chunks = list(_chunks(symbols))
        if self._workers > 1 and len(chunks) > 1:
            with ThreadPoolExecutor(max_workers=self._workers) as pool:
                results = list(pool.map(chunk_bars, chunks))
        else:
            results = [chunk_bars(chunk) for chunk in chunks]
        for result in results:
            yield from result

    def _pages(self, url: str, params: Params) -> Iterator[dict[str, Any]]:
        token: str | None = None
        while True:
            body = self._http.get_json(url, {**params, "page_token": token})
            if not isinstance(body, dict):
                path = httpx.URL(url).path
                raise VendorError("alpaca", f"{path}: unexpected response", retryable=False)
            yield body
            token = body.get("next_page_token")
            if not token:
                return


def _corporate_action(key: str, item: dict[str, Any]) -> CorporateAction:
    kind, symbol_field = _BY_KEY[key]
    when = item.get("ex_date") or item.get("effective_date") or item["process_date"]
    process = item.get("process_date")
    ratio = None
    if kind in (CorporateActionKind.SPLIT, CorporateActionKind.REVERSE_SPLIT):
        ratio = float(item["new_rate"]) / float(item["old_rate"])
    terms = {k: v for k, v in item.items() if k != "id"}
    return CorporateAction(
        kind,
        item[symbol_field],
        date.fromisoformat(when),
        date.fromisoformat(process) if process else None,
        ratio,
        str(item["id"]),
        terms,
    )
