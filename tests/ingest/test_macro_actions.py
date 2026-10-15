from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, select, update

from auratrade.calendar.sessions import MarketCalendar
from auratrade.data.alpaca_data import CorporateAction, CorporateActionKind
from auratrade.data.macro import CboeHistory, CboeIndex, DelayedQuote, FredSeries
from auratrade.db.models import CorporateAction as CorporateActionRow
from auratrade.db.models import MacroObservation
from auratrade.ingest.corporate_actions import KINDS, ingest_corporate_actions
from auratrade.ingest.macro import MacroReport, ingest_macro, quote_is_final
from tests.ingest.fakes import RISK, add_ticker, sync_calendar

THU, WED, TUE = date(2026, 10, 15), date(2026, 10, 14), date(2026, 10, 13)
HALF_DAY = date(2026, 11, 27)


def et(day: date, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(
        day.year, day.month, day.day, hour, minute, second, tzinfo=ZoneInfo("America/New_York")
    ).astimezone(UTC)


@dataclass
class FakeCboe:
    quotes: dict[CboeIndex, DelayedQuote] = field(default_factory=dict)
    histories: dict[CboeIndex, dict[date, float]] = field(default_factory=dict)
    error: Exception | None = None

    def delayed_quote(self, index: CboeIndex) -> DelayedQuote:
        if self.error is not None:
            raise self.error
        return self.quotes[index]

    def history(self, index: CboeIndex) -> CboeHistory:
        if self.error is not None:
            raise self.error
        return CboeHistory(index, self.histories.get(index, {}))


@dataclass
class FakeFred:
    series: dict[FredSeries, dict[date, float]] = field(default_factory=dict)

    def observations(self, series: FredSeries, start: date, end: date) -> dict[date, float]:
        return {d: v for d, v in self.series.get(series, {}).items() if start <= d <= end}


def quote(index: CboeIndex, close: float, at: datetime) -> DelayedQuote:
    return DelayedQuote(index, close, close, close - 0.5, at)


@pytest.fixture
def calendar(pg_engine: Engine) -> MarketCalendar:
    return sync_calendar(pg_engine)


def macro(engine: Engine) -> dict[tuple[str, date], tuple[float, str]]:
    with engine.connect() as conn:
        rows = conn.execute(select(MacroObservation.__table__)).all()
    return {(r.series, r.observation_date): (r.value, r.source) for r in rows}


def run_macro(
    engine: Engine, calendar: MarketCalendar, cboe: FakeCboe, fred: FakeFred | None, day: date = THU
) -> MacroReport:
    with engine.begin() as conn:
        return ingest_macro(conn, cboe, fred, calendar, RISK, day)


FINAL = {
    CboeIndex.VIX: quote(CboeIndex.VIX, 16.76, et(THU, 16, 15, 1)),
    CboeIndex.VIX3M: quote(CboeIndex.VIX3M, 18.9, et(THU, 16, 15, 1)),
}


class TestMacro:
    def test_final_quote_is_tonights_value(
        self, pg_engine: Engine, calendar: MarketCalendar
    ) -> None:
        history = {CboeIndex.VIX: {WED: 16.87}, CboeIndex.VIX3M: {WED: 19.0}}
        fred = FakeFred({FredSeries.CREDIT_SPREAD: {TUE: 3.1, WED: 3.0}})
        report = run_macro(pg_engine, calendar, FakeCboe(FINAL, history), fred)
        assert report.sources == {
            "VIX": "CBOE_QUOTE",
            "VIX3M": "CBOE_QUOTE",
            "CREDIT_SPREAD": "FRED",
        }
        stored = macro(pg_engine)
        assert stored[("VIX", THU)] == (16.76, "CBOE_QUOTE")
        assert stored[("VIX", WED)] == (16.87, "CBOE_HISTORY")
        assert stored[("CREDIT_SPREAD", WED)] == (3.0, "FRED")
        assert report.missing == []
        assert run_macro(pg_engine, calendar, FakeCboe(FINAL, history), fred).written == 0

    def test_unsettled_quote_falls_back_to_fred(
        self, pg_engine: Engine, calendar: MarketCalendar
    ) -> None:
        early = {i: quote(i, 17.0, et(THU, 16, 13)) for i in FINAL}
        fred = FakeFred({FredSeries.VIX: {WED: 16.8}, FredSeries.VIX3M: {WED: 18.95}})
        report = run_macro(pg_engine, calendar, FakeCboe(early), fred)
        assert report.sources["VIX"] == "FRED"
        stored = macro(pg_engine)
        assert ("VIX", THU) not in stored
        assert stored[("VIX", WED)] == (16.8, "FRED")

    def test_history_file_with_tonights_row(
        self, pg_engine: Engine, calendar: MarketCalendar
    ) -> None:
        history = {CboeIndex.VIX: {THU: 16.7}, CboeIndex.VIX3M: {THU: 18.8}}
        report = run_macro(pg_engine, calendar, FakeCboe({}, history), None)
        assert report.sources == {"VIX": "CBOE_HISTORY", "VIX3M": "CBOE_HISTORY"}

    def test_nothing_final_leaves_the_session_missing(
        self, pg_engine: Engine, calendar: MarketCalendar
    ) -> None:
        report = run_macro(pg_engine, calendar, FakeCboe(error=RuntimeError("cdn down")), None)
        assert report.missing == ["VIX", "VIX3M"]
        assert len(report.errors) == 4
        assert macro(pg_engine) == {}

    def test_next_evening_history_confirms_and_logs_differences(
        self, pg_engine: Engine, calendar: MarketCalendar
    ) -> None:
        run_macro(pg_engine, calendar, FakeCboe(FINAL), None, THU)
        fri = date(2026, 10, 16)
        later = {i: quote(i, 15.0, et(fri, 16, 15, 1)) for i in FINAL}
        history = {CboeIndex.VIX: {THU: 16.76}, CboeIndex.VIX3M: {THU: 18.7}}
        report = run_macro(pg_engine, calendar, FakeCboe(later, history), None, fri)
        stored = macro(pg_engine)
        assert stored[("VIX", THU)] == (16.76, "CBOE_HISTORY")
        assert stored[("VIX3M", THU)] == (18.7, "CBOE_HISTORY")
        assert report.mismatches == ["VIX3M 2026-10-15: quote 18.9 vs history 18.7"]

    def test_lower_ranked_source_never_overwrites(
        self, pg_engine: Engine, calendar: MarketCalendar
    ) -> None:
        history = {CboeIndex.VIX: {WED: 16.87}, CboeIndex.VIX3M: {WED: 19.0}}
        run_macro(pg_engine, calendar, FakeCboe(FINAL, history), None)
        early = {i: quote(i, 17.0, et(THU, 16, 13)) for i in FINAL}
        fred = FakeFred({FredSeries.VIX: {WED: 99.0}, FredSeries.VIX3M: {WED: 99.0}})
        run_macro(pg_engine, calendar, FakeCboe(early), fred)
        assert macro(pg_engine)[("VIX", WED)] == (16.87, "CBOE_HISTORY")

    def test_holiday_rows_are_ignored(self, pg_engine: Engine, calendar: MarketCalendar) -> None:
        good_friday = date(2026, 4, 3)
        history = {CboeIndex.VIX: {good_friday: 20.0, date(2026, 4, 6): 19.0}}
        run_macro(pg_engine, calendar, FakeCboe({}, history), None, date(2026, 4, 6))
        stored = macro(pg_engine)
        assert ("VIX", good_friday) not in stored
        assert stored[("VIX", date(2026, 4, 6))] == (19.0, "CBOE_HISTORY")


def test_quote_is_final_on_a_half_day() -> None:
    close = et(HALF_DAY, 13, 0)
    assert quote_is_final(quote(CboeIndex.VIX, 1, et(HALF_DAY, 13, 15, 1)), HALF_DAY, close, 15)
    assert not quote_is_final(quote(CboeIndex.VIX, 1, et(HALF_DAY, 13, 14)), HALF_DAY, close, 15)
    yesterday = et(date(2026, 11, 25), 16, 15, 1)
    assert not quote_is_final(quote(CboeIndex.VIX, 1, yesterday), HALF_DAY, close, 15)


# --- corporate actions -------------------------------------------------------------------


@dataclass
class FakeActions:
    actions: list[CorporateAction]
    calls: list[tuple[date, date, tuple[CorporateActionKind, ...]]] = field(default_factory=list)

    def corporate_actions(
        self,
        start: date,
        end: date,
        *,
        symbols: Iterable[str] | None = None,
        kinds: Iterable[CorporateActionKind] = (),
    ) -> list[CorporateAction]:
        self.calls.append((start, end, tuple(kinds)))
        return self.actions


def action(
    symbol: str, kind: CorporateActionKind, ex: date, ratio: float | None = None, sid: str = "1"
) -> CorporateAction:
    return CorporateAction(kind, symbol, ex, ex, ratio, sid, {"note": symbol})


class TestCorporateActions:
    def test_padded_window_known_tickers_and_idempotence(
        self, pg_engine: Engine, calendar: MarketCalendar
    ) -> None:
        with pg_engine.begin() as conn:
            aaa = add_ticker(conn, "AAA")
            add_ticker(conn, "BBB")
        source = FakeActions(
            [
                action("AAA", CorporateActionKind.SPLIT, date(2026, 10, 19), 3.0, "a1"),
                action("BBB", CorporateActionKind.SYMBOL_CHANGE, THU, None, "b1"),
                action("ZZZ", CorporateActionKind.REVERSE_SPLIT, THU, 0.1, "z1"),
            ]
        )
        with pg_engine.begin() as conn:
            report = ingest_corporate_actions(conn, source, calendar, RISK, THU)
        assert report.window == (date(2026, 10, 8), date(2026, 10, 22))
        assert source.calls[0][2] == KINDS
        assert CorporateActionKind.CASH_DIVIDEND not in KINDS
        assert (report.fetched, report.stored) == (3, 2)
        assert report.upcoming_splits == ["AAA 2026-10-19 x3"]
        with pg_engine.connect() as conn:
            rows = {r.source_id: r for r in conn.execute(select(CorporateActionRow.__table__))}
        assert set(rows) == {"a1", "b1"}
        assert (rows["a1"].ticker_id, rows["a1"].ratio, rows["a1"].ex_date) == (
            aaa,
            3.0,
            date(2026, 10, 19),
        )

        with pg_engine.begin() as conn:
            conn.execute(
                update(CorporateActionRow).values(handled=True, handled_at=datetime.now(UTC))
            )
            again = ingest_corporate_actions(conn, source, calendar, RISK, THU)
        assert again.stored == 0
        with pg_engine.connect() as conn:
            assert all(r.handled for r in conn.execute(select(CorporateActionRow.__table__)))
