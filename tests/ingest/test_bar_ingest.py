"""The bars step on Postgres for the ETF list, against an in-memory vendor and Alpaca's recorded
calendar."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import Engine, select

from auratrade.calendar.sessions import MarketCalendar
from auratrade.config import RiskConfig
from auratrade.data.alpaca_data import Adjustment
from auratrade.db.models import BarCorrection, DailyBar, MarketSession
from auratrade.ingest.bar_ingest import (
    BarReport,
    MissingTickerError,
    ingest_bars,
    required_symbols,
    tracked_symbols,
)
from tests.ingest.fakes import FUNDS, NOW, FakeBars, add_ticker, small_risk, sync_calendar

RISK = small_risk()
THU = date(2026, 10, 15)
FRI = date(2026, 10, 16)
LABOR_DAY_TUE = date(2026, 9, 8)  # Monday 09-07 is Labor Day
THANKSGIVING_FRI = date(2026, 11, 27)  # early close


@pytest.fixture
def calendar(pg_engine: Engine) -> MarketCalendar:
    return sync_calendar(pg_engine)


def sessions(calendar: MarketCalendar, start: date, end: date) -> list[date]:
    return [s.day for s in calendar.sessions_between(start, end)]


@pytest.fixture
def world(pg_engine: Engine, calendar: MarketCalendar) -> tuple[FakeBars, dict[str, int]]:
    days = sessions(calendar, date(2026, 7, 1), date(2026, 12, 31))
    vendor = FakeBars()
    ids = {}
    with pg_engine.begin() as conn:
        for symbol in FUNDS:
            ids[symbol] = add_ticker(conn, symbol, "ETF", "ARCA")
        ids["AAA"] = add_ticker(conn, "AAA")  # a stock: not on the list
    for symbol in (*FUNDS, "AAA"):
        vendor.add(symbol, days, price=50.0, step=0.1)
    return vendor, ids


def run(
    engine: Engine,
    vendor: FakeBars,
    calendar: MarketCalendar,
    session: date,
    risk: RiskConfig = RISK,
) -> BarReport:
    with engine.begin() as conn:
        return ingest_bars(conn, vendor, calendar, risk, session, NOW)


def stored(engine: Engine, ticker_id: int) -> dict[date, DailyBar]:
    with engine.connect() as conn:
        rows = conn.execute(select(DailyBar).where(DailyBar.ticker_id == ticker_id)).all()
    return {r.session_date: r for r in rows}


def status(engine: Engine, day: date) -> tuple[str, float | None]:
    with engine.connect() as conn:
        row = conn.execute(
            select(MarketSession.data_status, MarketSession.coverage).where(
                MarketSession.session_date == day
            )
        ).one()
    return row.data_status, row.coverage


def test_the_etf_list() -> None:
    assert tracked_symbols(RISK) == list(FUNDS)
    assert required_symbols(RISK) == ["QQQ", "SPY"]  # the market fund and the indexes
    full = tracked_symbols(small_risk().model_copy(update={"portfolio": RISK.portfolio}))
    assert {"SPY", "SSO", "BIL", "QQQ"} <= set(full)


class TestFirstRun:
    def test_the_list_gets_its_window(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        report = run(pg_engine, vendor, calendar, THU)
        assert report.tracked == len(FUNDS)
        window = sessions(calendar, date(2026, 9, 1), THU)[-20:]
        assert len(window) == 20
        for symbol in FUNDS:
            assert sorted(stored(pg_engine, ids[symbol])) == window
        assert stored(pg_engine, ids["AAA"]) == {}
        assert report.status == "COMPLETE"
        assert report.coverage == 1.0
        assert status(pg_engine, THU) == ("COMPLETE", 1.0)
        spy = stored(pg_engine, ids["SPY"])[THU]
        assert spy.raw_close == pytest.approx(spy.close)
        assert all(call[0] == FUNDS for call in vendor.calls)

    def test_second_run_is_a_no_op(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        first = run(pg_engine, vendor, calendar, THU)
        assert first.rows_written == len(FUNDS) * 20
        before = stored(pg_engine, ids["SPY"])
        second = run(pg_engine, vendor, calendar, THU)
        assert second.rows_written == 0
        assert second.fetched_full == 0
        assert stored(pg_engine, ids["SPY"]) == before

    def test_a_fund_not_in_tickers_fails_closed(
        self, pg_engine: Engine, calendar: MarketCalendar
    ) -> None:
        with pg_engine.begin() as conn:
            for symbol in FUNDS:
                if symbol != "SSO":
                    add_ticker(conn, symbol, "ETF", "ARCA")
        with pytest.raises(MissingTickerError, match="SSO"):
            run(pg_engine, FakeBars(), calendar, THU)


class TestEveningUpdates:
    def test_next_session_refetches_t_minus_1(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        run(pg_engine, vendor, calendar, THU)
        # Thursday's volume grew after the evening it was fetched (#118).
        vendor.set_bar("SPY", THU, volume=180_000)
        vendor.calls.clear()
        report = run(pg_engine, vendor, calendar, FRI)
        bars = stored(pg_engine, ids["SPY"])
        assert bars[THU].volume == 180_000
        assert FRI in bars
        assert report.rows_written == len(FUNDS) + 1  # the Friday bars, SPY's Thursday
        assert {(start, end) for _, start, end, _ in vendor.calls} == {(THU, FRI)}

    def test_monday_holiday(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        friday = date(2026, 9, 4)
        run(pg_engine, vendor, calendar, friday)
        vendor.calls.clear()
        report = run(pg_engine, vendor, calendar, LABOR_DAY_TUE)
        assert date(2026, 9, 7) not in stored(pg_engine, ids["SPY"])
        assert LABOR_DAY_TUE in stored(pg_engine, ids["SPY"])
        assert report.status == "COMPLETE"
        assert {start for _, start, _, _ in vendor.calls} == {friday}

    def test_day_after_thanksgiving(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        run(pg_engine, vendor, calendar, date(2026, 11, 25))
        report = run(pg_engine, vendor, calendar, THANKSGIVING_FRI)
        assert calendar.is_early_close(THANKSGIVING_FRI)
        assert date(2026, 11, 26) not in stored(pg_engine, ids["SPY"])
        assert THANKSGIVING_FRI in stored(pg_engine, ids["SPY"])
        assert report.status == "COMPLETE"


class TestValidation:
    def test_a_missing_market_fund_bar_is_incomplete(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, _ = world
        run(pg_engine, vendor, calendar, THU)
        vendor.hidden = {("SPY", FRI)}
        report = run(pg_engine, vendor, calendar, FRI)
        assert report.retried == 1
        assert (report.missing, report.missing_required) == (["SPY"], ["SPY"])
        assert report.status == "INCOMPLETE"
        assert status(pg_engine, FRI) == ("INCOMPLETE", 0.8)

    def test_a_missing_index_bar_is_incomplete(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, _ = world
        run(pg_engine, vendor, calendar, THU)
        vendor.hidden = {("QQQ", FRI)}
        report = run(pg_engine, vendor, calendar, FRI)
        assert report.missing_required == ["QQQ"]
        assert report.status == "INCOMPLETE"

    def test_a_missing_bar_recovered_by_the_retry(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        run(pg_engine, vendor, calendar, THU)
        vendor.hidden = {("SPY", FRI)}
        vendor.hide_for_calls = 2  # the first fetch (split + raw)
        report = run(pg_engine, vendor, calendar, FRI)
        assert report.retried == 1
        assert report.status == "COMPLETE"
        assert FRI in stored(pg_engine, ids["SPY"])

    def test_another_fund_missing_is_reported_and_stays_complete(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        run(pg_engine, vendor, calendar, THU)
        vendor.hidden = {("GLD", FRI)}
        report = run(pg_engine, vendor, calendar, FRI)
        assert (report.missing, report.missing_required) == (["GLD"], [])
        assert report.coverage == 0.8
        assert report.status == "COMPLETE"
        assert FRI not in stored(pg_engine, ids["GLD"])  # never forward-filled

    def test_split_refetches_the_window(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        run(pg_engine, vendor, calendar, THU)
        before = stored(pg_engine, ids["SSO"])
        spy_before = stored(pg_engine, ids["SPY"])
        vendor.splits["SSO"] = (FRI, 2.0)  # 2-for-1, ex-date Friday
        report = run(pg_engine, vendor, calendar, FRI)
        assert report.splits == ["SSO"]
        after = stored(pg_engine, ids["SSO"])
        assert len(after) == 20
        day = date(2026, 10, 1)
        assert after[day].close == pytest.approx(before[day].close / 2, rel=1e-6)
        assert after[day].volume == before[day].volume * 2
        assert after[day].raw_close == pytest.approx(before[day].raw_close)  # as traded
        spy = stored(pg_engine, ids["SPY"])
        assert {d: spy[d] for d in spy_before} == spy_before  # an unsplit fund is untouched

    def test_zero_volume_bars_are_dropped(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        vendor.set_bar("GLD", THU, volume=0)
        report = run(pg_engine, vendor, calendar, THU)
        assert THU not in stored(pg_engine, ids["GLD"])
        assert report.dropped == {"zero volume": 2}  # the fetch and its retry
        assert report.missing == ["GLD"]

    def test_outlier_high_is_corrected_and_recorded(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        bad_day = date(2026, 10, 7)
        vendor.set_bar("GLD", bad_day, high=95.0)
        report = run(pg_engine, vendor, calendar, THU)
        assert report.corrections == 1
        bar = stored(pg_engine, ids["GLD"])[bad_day]
        assert bar.high == pytest.approx(max(bar.open, bar.close))
        with pg_engine.connect() as conn:
            [c] = conn.execute(select(BarCorrection.__table__)).all()
        assert (c.ticker_id, c.session_date, c.field, c.original) == (
            ids["GLD"],
            bad_day,
            "HIGH",
            95.0,
        )

    def test_newest_bar_outlier_rechecked_with_both_neighbours(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        # Thursday's high is far above its body and Wednesday's high; Friday trades up there
        # too, so the second evening sees a real move and restores the vendor's high.
        vendor.set_bar("GLD", THU, high=70.0)
        run(pg_engine, vendor, calendar, THU)
        assert stored(pg_engine, ids["GLD"])[THU].high < 70.0
        vendor.set_bar("GLD", FRI, open=66.0, high=72.0, low=65.0, close=70.0)
        run(pg_engine, vendor, calendar, FRI)
        assert stored(pg_engine, ids["GLD"])[THU].high == pytest.approx(70.0)


def test_raw_and_split_adjusted_requests(
    pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
) -> None:
    vendor, _ = world
    run(pg_engine, vendor, calendar, THU)
    assert {c[3] for c in vendor.calls} == {Adjustment.SPLIT, Adjustment.RAW}
