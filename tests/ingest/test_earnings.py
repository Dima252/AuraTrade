from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, insert, select

from auratrade.calendar.sessions import MarketCalendar
from auratrade.data.edgar import Filing, Submissions
from auratrade.data.finnhub import CalendarEntry, EarningsTiming, Surprise
from auratrade.data.news import NewsItem
from auratrade.db.models import EarningsEvent
from auratrade.ingest.earnings import (
    closest_headline,
    ingest_earnings,
    match_surprise,
    moment_timing,
    resolve_timing,
    usual_timing,
)
from tests.ingest.fakes import NOW, RISK, add_ticker, sync_calendar

NY = ZoneInfo("America/New_York")
THU, WED, FRI = date(2026, 10, 15), date(2026, 10, 14), date(2026, 10, 16)


def et(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY).astimezone(UTC)


@pytest.fixture
def calendar(pg_engine: Engine) -> MarketCalendar:
    return sync_calendar(pg_engine)


class TestTiming:
    def test_moment_timing(self, calendar: MarketCalendar) -> None:
        assert moment_timing(et(THU, 7, 30), calendar) == (THU, "BEFORE_OPEN")
        assert moment_timing(et(THU, 9, 30), calendar) == (THU, "DURING_MARKET")
        assert moment_timing(et(THU, 15, 59), calendar) == (THU, "DURING_MARKET")
        assert moment_timing(et(THU, 16, 0), calendar) == (THU, "AFTER_CLOSE")
        assert moment_timing(et(date(2026, 10, 17), 10), calendar) == (
            date(2026, 10, 19),
            "BEFORE_OPEN",
        )  # Saturday: before Monday's open
        half_day = date(2026, 11, 27)
        assert moment_timing(et(half_day, 13, 5), calendar) == (half_day, "AFTER_CLOSE")

    def test_closest_headline(self) -> None:
        accepted = et(THU, 16, 30)

        def item(headline: str, at: datetime, i: int) -> NewsItem:
            return NewsItem(i, headline, "", "benzinga", "", ("AAA",), at, at, None)

        items = [
            item("AAA Q3 earnings preview", et(date(2026, 10, 10), 9), 1),  # too early
            item("AAA reports Q3 results", et(THU, 16, 5), 2),
            item("AAA beats on revenue", et(THU, 16, 45), 3),
            item("AAA names new CFO", et(THU, 16, 31), 4),  # no earnings word
        ]
        assert closest_headline(items, accepted) == et(THU, 16, 45)
        assert closest_headline(items[:1], accepted) is None

    def test_usual_and_resolved_timing(self) -> None:
        amc: EarningsTiming = "AFTER_CLOSE"
        bmo: EarningsTiming = "BEFORE_OPEN"
        assert usual_timing([amc, amc, bmo]) == amc
        assert usual_timing([amc, bmo]) == bmo  # a tie goes to the earlier cushion
        assert usual_timing(["UNKNOWN"]) is None
        assert resolve_timing(amc, [bmo]) == (amc, False)
        assert resolve_timing("UNKNOWN", [amc, amc]) == (amc, True)
        assert resolve_timing("UNKNOWN", []) == (bmo, True)

    def test_match_surprise(self) -> None:
        s1 = Surprise(date(2026, 6, 30), 2026, 2, 1.0, 0.9, 11.1)
        s2 = Surprise(date(2026, 9, 30), 2026, 3, 1.2, 1.1, 9.1)
        assert match_surprise([s1, s2], 2026, 2, THU) == s1
        assert match_surprise([s1, s2], None, None, THU) == s2
        assert match_surprise([s1, s2], None, None, date(2026, 7, 15)) == s1
        assert match_surprise([s2], None, None, date(2026, 9, 1)) is None


# --- the step ----------------------------------------------------------------------------


@dataclass
class FakeFinnhub:
    days: dict[date, list[CalendarEntry]] = field(default_factory=dict)
    surprises: dict[str, list[Surprise]] = field(default_factory=dict)
    failing: set[date] = field(default_factory=set)
    surprise_calls: list[str] = field(default_factory=list)

    def earnings_calendar(self, day: date) -> list[CalendarEntry]:
        if day in self.failing:
            raise RuntimeError("finnhub 502")
        return self.days.get(day, [])

    def earnings_surprises(self, symbol: str) -> list[Surprise]:
        self.surprise_calls.append(symbol)
        return self.surprises.get(symbol, [])


@dataclass
class FakeNews:
    items: list[NewsItem] = field(default_factory=list)

    def news(self, symbols: Iterable[str], start: datetime, end: datetime) -> list[NewsItem]:
        wanted = set(symbols)
        return [n for n in self.items if wanted & set(n.symbols) and start <= n.created_at <= end]


def entry(
    symbol: str, day: date, timing: EarningsTiming = "UNKNOWN", **kw: object
) -> CalendarEntry:
    values: dict[str, object] = {
        "fiscal_year": 2026,
        "fiscal_quarter": 3,
        "eps_estimate": 1.0,
        "eps_actual": None,
        "revenue_estimate": 100,
        "revenue_actual": None,
    } | kw
    return CalendarEntry(symbol, day, timing, **values)  # type: ignore[arg-type]


def submissions_for(accepted: datetime) -> Submissions:
    filing = Filing("0001", "8-K", accepted.date(), accepted, None, ("2.02", "9.01"), "x.htm")
    other = Filing("0002", "8-K", accepted.date(), accepted, None, ("5.02",), "y.htm")
    return Submissions(1, "AAA Inc", "3674", None, ("AAA",), ("Nasdaq",), (filing, other))


def events(engine: Engine) -> dict[tuple[str, date], EarningsEvent]:
    with engine.connect() as conn:
        rows = conn.execute(select(EarningsEvent.__table__)).all()
    return {(r.source, r.report_date): r for r in rows}  # type: ignore[misc]


def run(
    engine: Engine,
    calendar: MarketCalendar,
    finnhub: FakeFinnhub,
    subs: dict[int, Submissions],
    news: FakeNews | None = None,
    risk=RISK,
):  # type: ignore[no-untyped-def]
    with engine.begin() as conn:
        return ingest_earnings(
            conn, finnhub, subs.get, news or FakeNews(), calendar, risk, THU, NOW
        )


class TestStep:
    def test_upcoming_dates_and_inferred_timing(
        self, pg_engine: Engine, calendar: MarketCalendar
    ) -> None:
        with pg_engine.begin() as conn:
            aaa = add_ticker(conn, "AAA")
            add_ticker(conn, "BBB")
            add_ticker(conn, "ETFX", "ETF", "ARCA")
            for d in [date(2026, 1, 27), date(2026, 4, 28), date(2026, 7, 28)]:
                conn.execute(
                    insert(EarningsEvent).values(
                        ticker_id=aaa, report_date=d, timing="AFTER_CLOSE", source="EDGAR"
                    )
                )
        later = date(2026, 10, 27)
        finnhub = FakeFinnhub(
            days={
                later: [
                    entry("AAA", later),
                    entry("BBB", later),
                    entry("ETFX", later),
                    entry("ZZZ", later),
                ],
                date(2026, 10, 28): [entry("CCC", date(2026, 10, 28), "AFTER_CLOSE")],
            }
        )
        report = run(pg_engine, calendar, finnhub, {})
        end = calendar.sessions_between(THU, date(2027, 1, 29))[63].day  # 63 sessions ahead
        assert report.horizon == (THU, end)
        assert report.days_fetched == (end - THU).days + 1
        assert (report.upcoming, report.inferred) == (2, 2)
        with pg_engine.connect() as conn:
            found = conn.execute(select(EarningsEvent.__table__)).all()
        rows = {r.ticker_id: r for r in found if r.source == "FINNHUB"}
        assert (rows[aaa].timing, rows[aaa].timing_inferred) == ("AFTER_CLOSE", True)
        bbb = next(r for t, r in rows.items() if t != aaa)
        assert (bbb.timing, bbb.timing_inferred) == ("BEFORE_OPEN", True)
        assert run(pg_engine, calendar, finnhub, {}).moved == 0

    def test_moved_date_replaces_the_old_one_unless_a_day_failed(
        self, pg_engine: Engine, calendar: MarketCalendar
    ) -> None:
        with pg_engine.begin() as conn:
            add_ticker(conn, "AAA")
        first, moved = date(2026, 10, 27), date(2026, 10, 29)
        run(
            pg_engine, calendar, FakeFinnhub(days={first: [entry("AAA", first, "BEFORE_OPEN")]}), {}
        )
        failing = FakeFinnhub(
            days={moved: [entry("AAA", moved, "BEFORE_OPEN")]}, failing={date(2026, 11, 2)}
        )
        assert run(pg_engine, calendar, failing, {}).moved == 0
        assert {d for _, d in events(pg_engine)} == {first, moved}
        report = run(
            pg_engine, calendar, FakeFinnhub(days={moved: [entry("AAA", moved, "BEFORE_OPEN")]}), {}
        )
        assert report.moved == 1
        assert {d for _, d in events(pg_engine)} == {moved}

    def test_surprises_fill_actuals_within_the_cap(
        self, pg_engine: Engine, calendar: MarketCalendar
    ) -> None:
        with pg_engine.begin() as conn:
            for symbol in ("AAA", "BBB"):
                conn.execute(
                    insert(EarningsEvent).values(
                        ticker_id=add_ticker(conn, symbol),
                        report_date=WED,
                        timing="AFTER_CLOSE",
                        fiscal_year=2026,
                        fiscal_quarter=3,
                        eps_estimate=1.0,
                        source="FINNHUB",
                    )
                )
        finnhub = FakeFinnhub(
            surprises={"AAA": [Surprise(date(2026, 9, 30), 2026, 3, 1.25, 1.0, 25.0)]}
        )
        risk = RISK.model_copy(
            update={"ingest": RISK.ingest.model_copy(update={"earnings_surprise_max_requests": 1})}
        )
        report = run(pg_engine, calendar, finnhub, {}, risk=risk)
        assert (report.surprises, report.surprise_backlog) == (1, 1)
        row = events(pg_engine)[("FINNHUB", WED)]
        assert (row.eps_actual, row.eps_estimate, row.surprise_pct) == (1.25, 1.0, 25.0)
        # The calendar listing the report again without an actual keeps the stored one.
        again = FakeFinnhub(days={WED: [entry("AAA", WED, "AFTER_CLOSE")]})
        run(pg_engine, calendar, again, {}, risk=risk)
        assert any(r.eps_actual == 1.25 for r in events(pg_engine).values())

    def test_edgar_event_timed_by_the_earlier_headline(
        self, pg_engine: Engine, calendar: MarketCalendar
    ) -> None:
        with pg_engine.begin() as conn:
            aaa = add_ticker(conn, "AAA", cik=1)
            add_ticker(conn, "NOCIK")
            conn.execute(
                insert(EarningsEvent).values(
                    ticker_id=aaa, report_date=WED, timing="BEFORE_OPEN", source="FINNHUB"
                )
            )
        # The release went out at 07:55; the 8-K was filed after the close.
        news = FakeNews(
            [
                NewsItem(
                    1,
                    "AAA reports third-quarter results",
                    "",
                    "b",
                    "",
                    ("AAA",),
                    et(WED, 7, 55),
                    et(WED, 7, 55),
                    None,
                )
            ]
        )
        subs = {1: submissions_for(et(WED, 16, 40))}
        report = run(pg_engine, calendar, FakeFinnhub(), subs, news)
        assert report.edgar_events == 1
        row = events(pg_engine)[("EDGAR", WED)]
        assert (row.timing, row.accepted_at, row.headline_at) == (
            "BEFORE_OPEN",
            et(WED, 16, 40),
            et(WED, 7, 55),
        )
        assert run(pg_engine, calendar, FakeFinnhub(), subs, news).edgar_events == 0

    def test_edgar_event_without_a_headline(
        self, pg_engine: Engine, calendar: MarketCalendar
    ) -> None:
        with pg_engine.begin() as conn:
            aaa = add_ticker(conn, "AAA", cik=1)
            conn.execute(
                insert(EarningsEvent).values(ticker_id=aaa, report_date=WED, source="FINNHUB")
            )
        report = run(pg_engine, calendar, FakeFinnhub(), {1: submissions_for(et(WED, 16, 40))})
        assert report.edgar_events == 1
        assert events(pg_engine)[("EDGAR", WED)].timing == "AFTER_CLOSE"
