from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from auratrade.calendar.sessions import MarketCalendar
from auratrade.data.edgar import CompanyFacts, CompanyTicker, FactValue, Filing, Submissions
from auratrade.data.news import NewsItem
from auratrade.history.build import SECURITIES, SECURITY_SCHEMA
from auratrade.history.edgar import (
    EARNINGS,
    FUNDAMENTALS,
    NewsNotFetchedError,
    edgar_history,
)
from auratrade.history.news import fetch_news, fetched_through, missing_months, months, read_news
from auratrade.history.store import get_table, put_table, table_key
from auratrade.storage import LocalStore
from tests.calendar_fixture import alpaca_times, to_session

CALENDAR = MarketCalendar(to_session(s) for s in alpaca_times())
START, END = date(2026, 1, 1), date(2026, 2, 10)


def item(n: int, headline: str, at: datetime, *symbols: str) -> NewsItem:
    return NewsItem(n, headline, "", "", "", symbols, at, at, None)


# 07:30 New York on 2026-01-06: the press release, before the open.
RELEASE = item(
    1, "AAA Q4 EPS $1.10 Beats $1.00 Estimate", datetime(2026, 1, 6, 12, 30, tzinfo=UTC), "AAA"
)
CHATTER = item(2, "Analyst likes AAA", datetime(2026, 1, 6, 15, 0, tzinfo=UTC), "AAA")


@dataclass
class FakeNews:
    items: list[NewsItem]
    calls: list[datetime] = field(default_factory=list)

    def news(self, symbols: Iterable[str] | None, start: datetime, end: datetime) -> list[NewsItem]:
        assert symbols is None
        self.calls.append(start)
        return [n for n in self.items if start <= n.created_at <= end]


def test_months() -> None:
    assert months(date(2025, 11, 5), date(2026, 2, 1)) == [
        (2025, 11),
        (2025, 12),
        (2026, 1),
        (2026, 2),
    ]


def test_news_by_month_resumes(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    source = FakeNews([RELEASE, CHATTER])
    assert missing_months(store, START, END) == ["2026-01", "2026-02"]
    report = fetch_news(source, store, START, END, workers=2)
    assert (report.months, report.fetched, report.headlines) == (2, 2, 2)
    assert len(source.calls) == 31 + 10  # one request per calendar day
    assert fetched_through(store, 2026, 2) == END
    assert missing_months(store, START, END) == []

    again = fetch_news(source, store, START, END, workers=2)
    assert (again.fetched, again.skipped) == (0, 2)
    # A later end re-fetches only the unfinished month.
    later = fetch_news(source, store, START, date(2026, 2, 12), workers=2)
    assert (later.fetched, later.skipped) == (1, 1)
    assert [n.id for n in read_news(store, START, END)] == [1, 2]


def fact(start: date, end: date, value: float, filed: date, fp: str) -> FactValue:
    return FactValue(start, end, value, "acc", 2025, fp, "10-Q", filed, None)


def aaa_facts() -> CompanyFacts:
    revenue = [
        fact(date(2025, 1, 1), date(2025, 3, 31), 100.0, date(2025, 5, 1), "Q1"),
        fact(date(2025, 4, 1), date(2025, 6, 30), 120.0, date(2025, 8, 1), "Q2"),
    ]
    return CompanyFacts(1, "AAA", {"us-gaap": {"Revenues": {"USD": tuple(revenue)}}})


def filing(accession: str, form: str, at: datetime, items: tuple[str, ...] = ()) -> Filing:
    return Filing(accession, form, at.date(), at, None, items, "x.htm")


@dataclass
class FakeEdgar:
    calls: list[int] = field(default_factory=list)

    def company_tickers(self) -> list[CompanyTicker]:
        return [CompanyTicker(1, "AAA", "AAA Inc"), CompanyTicker(2, "BBB", "BBB Inc")]

    def submissions(self, cik: int, *, since: date | None = None) -> Submissions | None:
        self.calls.append(cik)
        if cik == 1:
            filings = (
                # Filed after the close, but the release came out before the open.
                filing("e1", "8-K", datetime(2026, 1, 6, 21, 5, tzinfo=UTC), ("2.02", "9.01")),
                # No headline: the 8-K alone, after the close.
                filing("e2", "8-K", datetime(2026, 1, 27, 21, 30, tzinfo=UTC), ("2.02",)),
                filing("x", "8-K", datetime(2026, 1, 8, 13, 0, tzinfo=UTC), ("5.02",)),
            )
            return Submissions(1, "AAA", "3674", None, ("AAA",), (), filings)
        # A 10-K filed before the first bar: it traded publicly before (an OTC uplisting).
        ten_k = filing("k", "10-K", datetime(2025, 12, 15, 21, 0, tzinfo=UTC))
        return Submissions(2, "BBB", None, None, ("BBB",), (), (ten_k,))

    def company_facts(self, cik: int) -> CompanyFacts | None:
        return aaa_facts() if cik == 1 else None


def security(key: str, symbol: str, active: bool, listed_at: date | None) -> dict[str, Any]:
    return {
        "security": key,
        "symbol": symbol,
        "alpaca_asset_id": key if active else None,
        "name": None,
        "exchange": None,
        "security_type": "COMMON",
        "type_assumed": False,
        "source": "ACTIVE" if active else "INACTIVE",
        "first_session": listed_at or START,
        "last_session": END,
        "active": active,
        "bars": 10,
        "relisted": False,
        "listed_at": listed_at,
        "cik": None,
        "sic": None,
    }


def test_edgar_history(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "store")
    work = tmp_path / "work"
    put_table(
        store,
        table_key(SECURITIES),
        pa.Table.from_pylist(
            [
                security("a-aaa", "AAA", True, None),
                security("a-bbb", "BBB", True, date(2026, 1, 20)),
                # Delisted: SEC's ticker map is today's, so it gets no CIK.
                security("i-old", "AAA", False, None),
            ],
            SECURITY_SCHEMA,
        ),
    )
    edgar = FakeEdgar()
    # Without the news the companies are still downloaded (cached), then it stops.
    with pytest.raises(NewsNotFetchedError):
        edgar_history(store, edgar, CALENDAR, START, END, work, workers=2)
    assert sorted(edgar.calls) == [1, 2]
    fetch_news(FakeNews([RELEASE, CHATTER]), store, START, END, workers=2)

    report = edgar_history(store, edgar, CALENDAR, START, END, work, workers=2)
    assert sorted(edgar.calls) == [1, 2]
    assert report.details() == {
        "securities_with_cik": 2,
        "companies": 2,
        "earnings_events": 2,
        "with_headline": 1,
        "fundamentals": 2,
        "listed_at_cleared": 1,
        "errors": [],
    }
    events = get_table(store, table_key(EARNINGS)).to_pylist()
    assert [(e["report_date"], e["timing"], e["headline_at"] is not None) for e in events] == [
        (date(2026, 1, 6), "BEFORE_OPEN", True),
        (date(2026, 1, 27), "AFTER_CLOSE", False),
    ]
    facts = get_table(store, table_key(FUNDAMENTALS)).to_pylist()
    assert [(f["security"], f["filed_date"]) for f in facts] == [
        ("a-aaa", date(2025, 5, 1)),
        ("a-aaa", date(2025, 8, 1)),
    ]
    securities = {s["security"]: s for s in get_table(store, table_key(SECURITIES)).to_pylist()}
    assert (securities["a-aaa"]["cik"], securities["a-aaa"]["sic"]) == (1, 3674)
    assert securities["a-bbb"]["listed_at"] is None
    assert securities["i-old"]["cik"] is None

    # Resumed from the work folder: no EDGAR call the second time.
    calls = len(edgar.calls)
    edgar_history(store, edgar, CALENDAR, START, END, work, workers=2)
    assert len(edgar.calls) == calls


def test_edgar_needs_the_securities_table(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    fetch_news(FakeNews([]), store, START, END, workers=1)
    with pytest.raises(FileNotFoundError):
        edgar_history(store, FakeEdgar(), CALENDAR, START, END, tmp_path / "w", workers=1)


def test_edgar_records_failed_companies(tmp_path: Path) -> None:
    class Failing(FakeEdgar):
        def company_facts(self, cik: int) -> CompanyFacts | None:
            raise RuntimeError("boom")

    store = LocalStore(tmp_path)
    fetch_news(FakeNews([]), store, START, END, workers=1)
    put_table(
        store,
        table_key(SECURITIES),
        pa.Table.from_pylist([security("a-aaa", "AAA", True, None)], SECURITY_SCHEMA),
    )
    report = edgar_history(store, Failing(), CALENDAR, START, END, tmp_path / "w", workers=1)
    assert (report.companies, report.errors) == (0, ["1: RuntimeError: boom"])
