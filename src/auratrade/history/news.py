"""Backfill stage: every Alpaca (Benzinga) headline since the history start, one object a month.

The EDGAR stage times each historical earnings report by the earlier of its 8-K and the closest
earnings headline (#119), and an EP news flag could be tested on the same record later (README
§17). Headlines only (no summaries or bodies): about 500-1,000 a day, 50 a request, so the
whole history is about 60,000 requests, five to six hours under Alpaca's 200 a minute. A month
records the day it was fetched through in the object's metadata and is fetched again only
until it is complete, so an interrupted run resumes.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Protocol

import pyarrow as pa
import structlog

from auratrade.calendar.sources import NY
from auratrade.data.news import NewsItem
from auratrade.history.store import from_bytes, news_key, put_table
from auratrade.storage import ObjectStore

log = structlog.get_logger(__name__)

NEWS_SCHEMA = pa.schema(
    [
        ("id", pa.int64()),
        ("created_at", pa.timestamp("us", tz="UTC")),
        ("headline", pa.string()),
        ("symbols", pa.list_(pa.string())),
    ]
)
FETCHED_THROUGH = b"fetched_through"


class MarketNews(Protocol):
    def news(
        self, symbols: Iterable[str] | None, start: datetime, end: datetime
    ) -> list[NewsItem]: ...


def months(start: date, end: date) -> list[tuple[int, int]]:
    out = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        out.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def _month_days(year: int, month: int, start: date, end: date) -> list[date]:
    first = max(date(year, month, 1), start)
    following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = min(following - timedelta(days=1), end)
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def fetched_through(store: ObjectStore, year: int, month: int) -> date | None:
    key = news_key(year, month)
    if not store.exists(key):
        return None
    meta = from_bytes(store.get(key)).schema.metadata or {}
    raw = meta.get(FETCHED_THROUGH)
    return date.fromisoformat(raw.decode()) if raw else None


@dataclass
class NewsReport:
    months: int = 0
    fetched: int = 0
    skipped: int = 0
    headlines: int = 0
    by_month: dict[str, int] = field(default_factory=dict)


def fetch_news(
    source: MarketNews,
    store: ObjectStore,
    start: date,
    end: date,
    *,
    workers: int,
) -> NewsReport:
    """Headlines created on calendar days [start, end] (New York dates), by month."""
    report = NewsReport()

    def day(d: date) -> list[NewsItem]:
        first = datetime.combine(d, time(0), tzinfo=NY)
        return source.news(None, first, first + timedelta(days=1) - timedelta(microseconds=1))

    for year, month in months(start, end):
        report.months += 1
        days = _month_days(year, month, start, end)
        done = fetched_through(store, year, month)
        if done is not None and done >= days[-1]:
            report.skipped += 1
            continue
        with ThreadPoolExecutor(max_workers=workers) as pool:
            items = [n for found in pool.map(day, days) for n in found]
        table = news_table(items).replace_schema_metadata(
            {FETCHED_THROUGH: days[-1].isoformat().encode()}
        )
        put_table(store, news_key(year, month), table)
        report.fetched += 1
        report.headlines += table.num_rows
        report.by_month[f"{year:04d}-{month:02d}"] = table.num_rows
        log.info("backfill_news_month", month=f"{year:04d}-{month:02d}", headlines=table.num_rows)
    return report


def news_table(items: Sequence[NewsItem]) -> pa.Table:
    unique = {n.id: n for n in items}
    rows = [
        {"id": n.id, "created_at": n.created_at, "headline": n.headline, "symbols": list(n.symbols)}
        for n in sorted(unique.values(), key=lambda n: (n.created_at, n.id))
    ]
    return pa.Table.from_pylist(rows, NEWS_SCHEMA)


def read_news(store: ObjectStore, start: date, end: date) -> list[NewsItem]:
    """Stored headlines of the months covering [start, end] (headline, symbols and time only)."""
    out = []
    for year, month in months(start, end):
        key = news_key(year, month)
        if not store.exists(key):
            continue
        for row in from_bytes(store.get(key)).to_pylist():
            out.append(
                NewsItem(
                    row["id"],
                    row["headline"],
                    "",
                    "",
                    "",
                    tuple(row["symbols"] or ()),
                    row["created_at"],
                    row["created_at"],
                    None,
                )
            )
    return out


def missing_months(store: ObjectStore, start: date, end: date) -> list[str]:
    """Months in [start, end] not fetched through their last day (or `end`)."""
    out = []
    for year, month in months(start, end):
        days = _month_days(year, month, start, end)
        done = fetched_through(store, year, month)
        if done is None or done < days[-1]:
            out.append(f"{year:04d}-{month:02d}")
    return out
