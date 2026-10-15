"""The bars step on Postgres, against an in-memory vendor and Alpaca's recorded calendar."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import Engine, select

from auratrade.calendar.sessions import MarketCalendar
from auratrade.config import RiskConfig
from auratrade.data.alpaca_data import Adjustment
from auratrade.db.models import BarCorrection, DailyBar, MarketSession
from auratrade.ingest.bar_ingest import BarReport, MissingIndexError, ingest_bars
from tests.ingest.fakes import NOW, FakeBars, add_ticker, small_risk, sync_calendar

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
        ids["SPY"] = add_ticker(conn, "SPY", "ETF", "ARCA")
        ids["QQQ"] = add_ticker(conn, "QQQ", "ETF", "NASDAQ")
        ids["AAA"] = add_ticker(conn, "AAA")
        ids["BBB"] = add_ticker(conn, "BBB")  # a $3 stock: never tracked
        ids["DDD"] = add_ticker(conn, "DDD", "ADR", "NYSE")
        ids["EEE"] = add_ticker(conn, "EEE", "ETF", "ARCA")  # not a universe type
        ids["FFF"] = add_ticker(conn, "FFF", exchange="BATS")  # not a universe exchange
    for symbol in ("SPY", "QQQ", "AAA", "DDD", "EEE", "FFF"):
        vendor.add(symbol, days, price=50.0, step=0.1)
    vendor.add("BBB", days, price=3.0)
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


class TestFirstRun:
    def test_tracked_set_gets_its_window(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        report = run(pg_engine, vendor, calendar, THU)
        assert (report.candidates, report.tracked) == (3, 4)  # AAA, BBB, DDD; + SPY, QQQ
        window = sessions(calendar, date(2026, 9, 1), THU)[-20:]
        assert len(window) == 20
        for symbol in ("SPY", "QQQ", "AAA", "DDD"):
            assert sorted(stored(pg_engine, ids[symbol])) == window
        for symbol in ("BBB", "EEE", "FFF"):
            assert stored(pg_engine, ids[symbol]) == {}
        assert report.status == "COMPLETE"
        assert report.coverage == 1.0
        assert status(pg_engine, THU) == ("COMPLETE", 1.0)
        aaa = stored(pg_engine, ids["AAA"])[THU]
        assert aaa.raw_close == pytest.approx(aaa.close)

    def test_second_run_is_a_no_op(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        first = run(pg_engine, vendor, calendar, THU)
        assert first.rows_written == 4 * 20
        before = stored(pg_engine, ids["AAA"])
        second = run(pg_engine, vendor, calendar, THU)
        assert second.rows_written == 0
        assert second.fetched_full == 0
        assert stored(pg_engine, ids["AAA"]) == before

    def test_missing_index_ticker_fails_closed(
        self, pg_engine: Engine, calendar: MarketCalendar
    ) -> None:
        with pg_engine.begin() as conn:
            add_ticker(conn, "SPY", "ETF", "ARCA")
        with pytest.raises(MissingIndexError, match="QQQ"):
            run(pg_engine, FakeBars(), calendar, THU)


class TestEveningUpdates:
    def test_next_session_refetches_t_minus_1(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        run(pg_engine, vendor, calendar, THU)
        # Thursday's volume grew after the evening it was fetched (#118).
        vendor.set_bar("AAA", THU, volume=180_000)
        vendor.calls.clear()
        report = run(pg_engine, vendor, calendar, FRI)
        bars = stored(pg_engine, ids["AAA"])
        assert bars[THU].volume == 180_000
        assert FRI in bars
        assert report.rows_written == 4 + 1  # four new Friday bars, AAA's Thursday
        gap_fill = [c for c in vendor.calls if c[0] == ("AAA", "DDD", "QQQ", "SPY")]
        assert {(start, end) for _, start, end, _ in gap_fill} == {(THU, FRI)}

    def test_monday_holiday(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        friday = date(2026, 9, 4)
        run(pg_engine, vendor, calendar, friday)
        vendor.calls.clear()
        report = run(pg_engine, vendor, calendar, LABOR_DAY_TUE)
        assert date(2026, 9, 7) not in stored(pg_engine, ids["AAA"])
        assert LABOR_DAY_TUE in stored(pg_engine, ids["AAA"])
        assert report.status == "COMPLETE"
        gap_fill = [c for c in vendor.calls if c[0] == ("AAA", "DDD", "QQQ", "SPY")]
        assert {start for _, start, _, _ in gap_fill} == {friday}

    def test_good_friday_gap(self, pg_engine: Engine, calendar: MarketCalendar) -> None:
        # Thursday 04-02, then Monday 04-06: no bar is expected (or invented) for 04-03.
        vendor = FakeBars()
        days = sessions(calendar, date(2026, 3, 1), date(2026, 4, 30))
        with pg_engine.begin() as conn:
            spy = add_ticker(conn, "SPY", "ETF", "ARCA")
            add_ticker(conn, "QQQ", "ETF", "NASDAQ")
        vendor.add("SPY", days)
        vendor.add("QQQ", days)
        run(pg_engine, vendor, calendar, date(2026, 4, 2))
        report = run(pg_engine, vendor, calendar, date(2026, 4, 6))
        assert date(2026, 4, 3) not in stored(pg_engine, spy)
        assert date(2026, 4, 6) in stored(pg_engine, spy)
        assert report.status == "COMPLETE"

    def test_day_after_thanksgiving(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        run(pg_engine, vendor, calendar, date(2026, 11, 25))
        report = run(pg_engine, vendor, calendar, THANKSGIVING_FRI)
        assert calendar.is_early_close(THANKSGIVING_FRI)
        assert date(2026, 11, 26) not in stored(pg_engine, ids["AAA"])
        assert THANKSGIVING_FRI in stored(pg_engine, ids["AAA"])
        assert report.status == "COMPLETE"

    def test_a_ticker_that_stops_passing_is_no_longer_fetched(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        run(pg_engine, vendor, calendar, THU)
        vendor.add("DDD", sessions(calendar, date(2026, 10, 1), FRI), price=2.0)
        report = run(pg_engine, vendor, calendar, FRI)
        assert report.tracked == 3
        assert FRI not in stored(pg_engine, ids["DDD"])
        assert THU in stored(pg_engine, ids["DDD"])  # kept until it ages out of the window

    def test_a_ticker_that_starts_passing_gets_its_window(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        run(pg_engine, vendor, calendar, THU)
        vendor.add("BBB", sessions(calendar, date(2026, 10, 9), FRI), price=30.0)
        report = run(pg_engine, vendor, calendar, FRI)
        assert report.fetched_full == 1
        assert len(stored(pg_engine, ids["BBB"])) == 20


class TestValidation:
    def test_vendor_short_count_is_incomplete(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, _ = world
        run(pg_engine, vendor, calendar, THU)
        vendor.hidden = {("AAA", FRI), ("DDD", FRI)}
        report = run(pg_engine, vendor, calendar, FRI)
        assert report.retried == 2
        assert report.coverage == 0.5
        assert report.status == "INCOMPLETE"
        assert status(pg_engine, FRI) == ("INCOMPLETE", 0.5)

    def test_short_count_recovered_by_the_retry(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        run(pg_engine, vendor, calendar, THU)
        vendor.hidden = {("AAA", FRI), ("DDD", FRI)}
        vendor.hide_for_calls = 3  # the screen and the first fetch (split + raw)
        report = run(pg_engine, vendor, calendar, FRI)
        assert report.retried == 2
        assert report.status == "COMPLETE"
        assert FRI in stored(pg_engine, ids["AAA"])

    def test_a_missing_index_bar_is_incomplete(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, _ = world
        run(pg_engine, vendor, calendar, THU)
        vendor.hidden = {("QQQ", FRI)}
        report = run(pg_engine, vendor, calendar, FRI, small_risk())
        assert report.missing_indexes == ["QQQ"]
        assert report.status == "INCOMPLETE"

    def test_a_halted_symbol_alone_stays_complete(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        run(pg_engine, vendor, calendar, THU)
        vendor.hidden = {("DDD", FRI)}
        risk = RISK.model_copy(update={"data": RISK.data.model_copy(update={"min_coverage": 0.7})})
        report = run(pg_engine, vendor, calendar, FRI, risk)
        assert report.coverage == 0.75
        assert report.status == "COMPLETE"
        assert FRI not in stored(pg_engine, ids["DDD"])  # never forward-filled

    def test_split_refetches_the_window(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        run(pg_engine, vendor, calendar, THU)
        before = stored(pg_engine, ids["AAA"])
        ddd_before = stored(pg_engine, ids["DDD"])
        vendor.splits["AAA"] = (FRI, 2.0)  # 2-for-1, ex-date Friday
        report = run(pg_engine, vendor, calendar, FRI)
        assert report.splits == ["AAA"]
        after = stored(pg_engine, ids["AAA"])
        assert len(after) == 20
        day = date(2026, 10, 1)
        assert after[day].close == pytest.approx(before[day].close / 2, rel=1e-6)
        assert after[day].volume == before[day].volume * 2
        assert after[day].raw_close == pytest.approx(before[day].raw_close)  # as traded
        ddd = stored(pg_engine, ids["DDD"])
        assert {d: ddd[d] for d in ddd_before} == ddd_before  # an unsplit ticker is untouched

    def test_zero_volume_bars_are_dropped(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        vendor.set_bar("AAA", THU, volume=0)
        risk = RISK.model_copy(update={"data": RISK.data.model_copy(update={"min_coverage": 0.7})})
        report = run(pg_engine, vendor, calendar, THU, risk)
        assert THU not in stored(pg_engine, ids["AAA"])
        assert report.dropped == {"zero volume": 1}

    def test_outlier_high_is_corrected_and_recorded(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        bad_day = date(2026, 10, 7)
        vendor.set_bar("AAA", bad_day, high=95.0)
        report = run(pg_engine, vendor, calendar, THU)
        assert report.corrections == 1
        bar = stored(pg_engine, ids["AAA"])[bad_day]
        assert bar.high == pytest.approx(max(bar.open, bar.close))
        with pg_engine.connect() as conn:
            [c] = conn.execute(select(BarCorrection.__table__)).all()
        assert (c.ticker_id, c.session_date, c.field, c.original) == (
            ids["AAA"],
            bad_day,
            "HIGH",
            95.0,
        )

    def test_newest_bar_outlier_rechecked_with_both_neighbours(
        self, pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
    ) -> None:
        vendor, ids = world
        # Thursday's high is far above its body and Wednesday's high; Friday trades up there
        # too, so the second evening sees a real breakout and restores the vendor's high.
        vendor.set_bar("AAA", THU, high=70.0)
        run(pg_engine, vendor, calendar, THU)
        assert stored(pg_engine, ids["AAA"])[THU].high < 70.0
        vendor.set_bar("AAA", FRI, open=66.0, high=72.0, low=65.0, close=70.0)
        run(pg_engine, vendor, calendar, FRI)
        assert stored(pg_engine, ids["AAA"])[THU].high == pytest.approx(70.0)


def test_raw_and_split_adjusted_requests(
    pg_engine: Engine, calendar: MarketCalendar, world: tuple[FakeBars, dict[str, int]]
) -> None:
    vendor, _ = world
    run(pg_engine, vendor, calendar, THU)
    screen = vendor.calls[0]
    assert screen[0] == ("AAA", "BBB", "DDD")
    assert screen[3] is Adjustment.RAW
    assert screen[1] == sessions(calendar, date(2026, 10, 9), THU)[0]
    adjustments = {c[3] for c in vendor.calls[1:]}
    assert adjustments == {Adjustment.SPLIT, Adjustment.RAW}
