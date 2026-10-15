from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import Engine, insert, select

from auratrade.calendar.sessions import MarketCalendar
from auratrade.data.edgar import CompanyTicker, Filing, Submissions
from auratrade.data.finnhub import Profile
from auratrade.db.models import DailyBar, IndustryGroup, Ticker
from auratrade.ingest.companies import (
    SubmissionsCache,
    group_for,
    ingest_companies,
    rollup,
    sec_symbol,
    session_index,
    sic_range,
    traded_before,
)
from tests.ingest.fakes import add_ticker, small_risk, sync_calendar

THU = date(2026, 10, 15)
RISK = small_risk(industry_min_group_size=2, industries_refresh_sessions=1)


def test_sec_symbol() -> None:
    assert sec_symbol("BRK.B") == "BRK-B"
    assert sec_symbol("AAPL") == "AAPL"


def test_rollup() -> None:
    counts = {3674: 5, 3672: 1, 3679: 1, 3711: 1, 2834: 3}
    assert rollup(counts, 3) == {
        3674: "3674",
        # A prefix group counts only the codes rolled into it (3674 has its own): 3672 and
        # 3679 make 2 at 367x, so they roll on to 36.
        3672: "36",
        3679: "36",
        3711: "37",  # alone at every level
        2834: "2834",
    }
    assert rollup(counts, 1) == {sic: f"{sic}" for sic in counts}


def test_ranges_and_lookup() -> None:
    assert sic_range("367") == [3670, 3679]
    assert sic_range("36") == [3600, 3699]
    assert sic_range("3674") == [3674, 3674]
    keys = {"3674", "367", "36"}
    assert group_for(3674, keys) == "3674"
    assert group_for(3672, keys) == "367"
    assert group_for(3600, keys) == "36"
    assert group_for(2834, keys) is None
    assert group_for(100, {"01"}) == "01"  # agriculture codes are zero-padded


def filing(form: str, filed: date) -> Filing:
    return Filing(
        "x", form, filed, datetime(filed.year, filed.month, filed.day, tzinfo=UTC), None, (), ""
    )


def subs(sic: str | None, *filings: Filing) -> Submissions:
    return Submissions(1, "Co", sic, None, (), (), tuple(filings))


def test_traded_before() -> None:
    first_bar = date(2026, 9, 1)
    assert traded_before(subs("1", filing("10-Q", date(2025, 8, 1))), first_bar)
    assert not traded_before(subs("1", filing("S-1", date(2026, 6, 1))), first_bar)
    assert not traded_before(subs("1", filing("10-Q", date(2026, 10, 1))), first_bar)


@dataclass
class FakeEdgar:
    tickers: list[CompanyTicker] = field(default_factory=list)
    by_cik: dict[int, Submissions] = field(default_factory=dict)
    calls: list[tuple[int, date | None]] = field(default_factory=list)

    def company_tickers(self) -> list[CompanyTicker]:
        return self.tickers

    def submissions(self, cik: int, *, since: date | None = None) -> Submissions | None:
        self.calls.append((cik, since))
        return self.by_cik.get(cik)


@dataclass
class FakeProfiles:
    industries: dict[str, str] = field(default_factory=dict)

    def profile(self, symbol: str) -> Profile | None:
        industry = self.industries.get(symbol)
        return None if industry is None else Profile(symbol, symbol, industry, None, None, None)


@pytest.fixture
def calendar(pg_engine: Engine) -> MarketCalendar:
    return sync_calendar(pg_engine)


def bars(conn: Any, ticker_id: int, days: list[date]) -> None:
    conn.execute(
        insert(DailyBar),
        [
            {
                "ticker_id": ticker_id,
                "session_date": d,
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "raw_close": 1,
                "volume": 1,
            }
            for d in days
        ],
    )


def run(
    engine: Engine, calendar: MarketCalendar, edgar: FakeEdgar, profiles: FakeProfiles | None = None
):  # type: ignore[no-untyped-def]
    with engine.begin() as conn:
        return ingest_companies(
            conn, edgar, SubmissionsCache(edgar, THU), profiles, calendar, RISK, THU
        )


def tickers(engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        return {r.symbol: r for r in conn.execute(select(Ticker.__table__))}


def test_companies_step(pg_engine: Engine, calendar: MarketCalendar) -> None:
    window = [s.day for s in calendar.sessions_between(date(2026, 9, 1), THU)][-20:]
    with pg_engine.begin() as conn:
        ids = {
            "AAA": add_ticker(conn, "AAA"),
            "BBB": add_ticker(conn, "BBB"),
            "BRK.B": add_ticker(conn, "BRK.B", exchange="NYSE"),
            "IPO": add_ticker(conn, "IPO"),
            "UPL": add_ticker(conn, "UPL"),
            "SHELL": add_ticker(conn, "SHELL"),
            "FRGN": add_ticker(conn, "FRGN", "ADR", "NYSE"),
            "QUIET": add_ticker(conn, "QUIET"),  # no bar tonight: not tracked
        }
        for symbol in ("AAA", "BBB", "BRK.B", "SHELL", "FRGN"):
            bars(conn, ids[symbol], window)
        for symbol in ("IPO", "UPL"):
            bars(conn, ids[symbol], window[10:])  # first bar inside the window
        bars(conn, ids["QUIET"], window[:5])
    edgar = FakeEdgar(
        tickers=[CompanyTicker(i + 1, sec_symbol(s), s) for i, s in enumerate(ids) if s != "FRGN"],
        by_cik={
            1: subs("3674"),
            2: subs("3674"),
            3: subs("6331"),
            4: subs("3672", filing("S-1", date(2026, 8, 1))),
            5: subs("2086", filing("10-K", date(2025, 3, 1))),  # traded OTC before (CELH-like)
            6: subs("6770"),
        },
    )
    report = run(pg_engine, calendar, edgar, FakeProfiles({"FRGN": "Semiconductors"}))
    rows = tickers(pg_engine)
    assert report.ciks_set == 7
    assert rows["BRK.B"].cik == 3
    assert rows["AAA"].sic == 3674
    assert rows["SHELL"].security_type == "SPAC"
    assert report.spacs == ["SHELL"]
    assert rows["IPO"].listed_at == window[10]
    assert rows["UPL"].listed_at is None
    assert report.traded_before_listing == ["UPL"]
    assert rows["AAA"].listed_at is None  # history starts at the window: unknown
    assert rows["FRGN"].finnhub_industry == "Semiconductors"
    assert all(since == THU for _, since in edgar.calls)

    with pg_engine.connect() as conn:
        groups = {g.id: g.name for g in conn.execute(select(IndustryGroup.__table__))}
    # Tracked SIC codes: 3674 (twice), 6331, 3672, 2086 (SHELL is a SPAC now: not a universe type).
    named = {
        s: groups.get(rows[s].industry_group_id) for s in ("AAA", "IPO", "BRK.B", "FRGN", "QUIET")
    }
    assert named == {
        "AAA": "SIC 3674",
        "IPO": "SIC 36",  # 3672 alone rolls up to 36
        "BRK.B": "SIC 63",
        "FRGN": "Finnhub: Semiconductors",
        "QUIET": None,
    }
    assert report.industry_coverage == 1.0

    again = run(pg_engine, calendar, edgar, FakeProfiles({"FRGN": "Semiconductors"}))
    assert (again.ciks_set, again.sic_changed, again.assigned, again.profiles) == (0, 0, 0, 0)


def test_session_index(calendar: MarketCalendar) -> None:
    assert session_index(calendar, calendar.first) == 0  # type: ignore[arg-type]
    assert session_index(calendar, date(2025, 12, 2)) == 1
