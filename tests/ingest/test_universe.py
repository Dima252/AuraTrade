from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import Engine, insert, select

from auratrade.calendar.sessions import MarketCalendar
from auratrade.data.alpaca_data import Asset, DailyBar
from auratrade.data.nasdaq import Listing
from auratrade.db.models import DailyBar as DailyBarRow
from auratrade.db.models import Ticker
from auratrade.ingest.classify import SecurityType
from auratrade.ingest.universe import (
    ListedSecurity,
    ScreenBar,
    TickerRefresh,
    UniverseError,
    listed_securities,
    passes_screen,
    refresh_tickers,
    screen_bars,
)
from tests.ingest.fakes import NOW, RISK, add_ticker, sync_calendar

THU = date(2026, 10, 15)


def asset(symbol: str, asset_id: str, name: str = "", exchange: str = "NASDAQ") -> Asset:
    name = name or f"{symbol} Inc. Common Stock"
    return Asset(asset_id, symbol, name, exchange, "active", True, ())


def sec(symbol: str, asset_id: str, kind: SecurityType = SecurityType.COMMON) -> ListedSecurity:
    return ListedSecurity(asset_id, symbol, f"{symbol} Inc. Common Stock", "NASDAQ", kind)


def aid(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


class TestListedSecurities:
    def test_filters_and_names(self) -> None:
        assets = [
            asset("AAPL", aid(1), "Apple Inc. Common Stock"),
            asset("SPY", aid(2), "SPDR S&P 500 ETF Trust", "ARCA"),
            Asset(aid(3), "OLD", "Old Co", "NASDAQ", "inactive", False, ()),
            Asset(aid(4), "HALT", "Halted Co", "NASDAQ", "active", False, ()),
            asset("OTCX", aid(5), "Pink Sheet Co", "OTC"),
            asset("BRK.B", aid(6), "BERKSHIRE HATHAWAY Class B", "NYSE"),
            asset("ZXZZT", aid(7), "Test", "NASDAQ"),
        ]
        listings = [
            Listing("SPY", "State Street SPDR S&P 500 ETF Trust", "NYSE Arca", True, False),
            Listing("BRK.B", "Berkshire Hathaway Inc. New Common Stock", "NYSE", False, False),
            Listing("ZXZZT", "NASDAQ TEST STOCK", "NASDAQ", False, True),
        ]
        out = {s.symbol: s for s in listed_securities(assets, listings)}
        assert set(out) == {"AAPL", "SPY", "BRK.B", "ZXZZT"}
        assert out["SPY"].security_type is SecurityType.ETF
        assert out["BRK.B"].name == "Berkshire Hathaway Inc. New Common Stock"
        assert out["BRK.B"].security_type is SecurityType.COMMON
        assert out["ZXZZT"].name == "Test"  # a test issue's directory row is ignored


@pytest.fixture
def calendar(pg_engine: Engine) -> MarketCalendar:
    return sync_calendar(pg_engine)


def refresh(
    engine: Engine,
    calendar: MarketCalendar,
    securities: list[ListedSecurity],
    max_delistings: int = 5,
) -> TickerRefresh:
    with engine.begin() as conn:
        return refresh_tickers(
            conn, securities, calendar, THU, now=NOW, max_delistings=max_delistings
        )


def tickers(engine: Engine) -> list[Ticker]:
    with engine.connect() as conn:
        return list(conn.execute(select(Ticker.__table__).order_by(Ticker.id)).all())  # type: ignore[arg-type]


class TestRefresh:
    def test_insert_then_no_op(self, pg_engine: Engine, calendar: MarketCalendar) -> None:
        listed = [sec("AAA", aid(1)), sec("SPY", aid(2), SecurityType.ETF)]
        assert refresh(pg_engine, calendar, listed) == TickerRefresh(2, 0, ())
        assert refresh(pg_engine, calendar, listed) == TickerRefresh(0, 0, ())
        rows = tickers(pg_engine)
        assert [(r.symbol, r.security_type, r.active) for r in rows] == [
            ("AAA", "COMMON", True),
            ("SPY", "ETF", True),
        ]

    def test_symbol_change_keeps_the_security(
        self, pg_engine: Engine, calendar: MarketCalendar
    ) -> None:
        refresh(pg_engine, calendar, [sec("FB", aid(1))])
        [before] = tickers(pg_engine)
        assert refresh(pg_engine, calendar, [sec("META", aid(1))]) == TickerRefresh(0, 1, ())
        [after] = tickers(pg_engine)
        assert (after.id, after.symbol) == (before.id, "META")

    def test_delisting_dates_from_the_last_bar(
        self, pg_engine: Engine, calendar: MarketCalendar
    ) -> None:
        refresh(pg_engine, calendar, [sec("AAA", aid(1)), sec("BBB", aid(2))])
        with pg_engine.begin() as conn:
            aaa = conn.execute(select(Ticker.id).where(Ticker.symbol == "AAA")).scalar_one()
            conn.execute(
                insert(DailyBarRow).values(
                    ticker_id=aaa,
                    session_date=date(2026, 10, 9),
                    open=1,
                    high=1,
                    low=1,
                    close=1,
                    raw_close=1,
                    volume=1,
                )
            )
        result = refresh(pg_engine, calendar, [])
        assert result.delisted == ("AAA", "BBB")
        rows = {r.symbol: r for r in tickers(pg_engine)}
        assert not rows["AAA"].active
        assert rows["AAA"].delisted_at == date(2026, 10, 12)  # the session after its last bar
        assert rows["BBB"].delisted_at == THU  # no bars: tonight

    def test_reused_symbol_is_a_new_security(
        self, pg_engine: Engine, calendar: MarketCalendar
    ) -> None:
        refresh(pg_engine, calendar, [sec("XYZ", aid(1))])
        result = refresh(pg_engine, calendar, [sec("XYZ", aid(2))])
        assert result == TickerRefresh(1, 0, ("XYZ",))
        old, new = tickers(pg_engine)
        assert (old.alpaca_asset_id, old.active) == (aid(1), False)
        assert (new.alpaca_asset_id, new.active) == (aid(2), True)

    def test_symbols_swapped_between_listed_securities(
        self, pg_engine: Engine, calendar: MarketCalendar
    ) -> None:
        refresh(pg_engine, calendar, [sec("AAA", aid(1)), sec("BBB", aid(2))])
        refresh(pg_engine, calendar, [sec("BBB", aid(1)), sec("AAA", aid(2))])
        rows = {r.alpaca_asset_id: r.symbol for r in tickers(pg_engine)}
        assert rows == {aid(1): "BBB", aid(2): "AAA"}

    def test_relisted_security_is_reactivated(
        self, pg_engine: Engine, calendar: MarketCalendar
    ) -> None:
        refresh(pg_engine, calendar, [sec("AAA", aid(1))])
        refresh(pg_engine, calendar, [])
        refresh(pg_engine, calendar, [sec("AAA", aid(1))])
        [row] = tickers(pg_engine)
        assert (row.active, row.delisted_at) == (True, None)

    def test_row_without_asset_id_is_adopted(
        self, pg_engine: Engine, calendar: MarketCalendar
    ) -> None:
        with pg_engine.begin() as conn:
            ticker_id = add_ticker(conn, "AAA")
        refresh(pg_engine, calendar, [sec("AAA", aid(1))])
        [row] = tickers(pg_engine)
        assert (row.id, row.alpaca_asset_id) == (ticker_id, aid(1))

    def test_blank_check_sic_overrides_the_name(
        self, pg_engine: Engine, calendar: MarketCalendar
    ) -> None:
        with pg_engine.begin() as conn:
            add_ticker(conn, "SPAQ", sic=6770, alpaca_asset_id=aid(1))
        refresh(pg_engine, calendar, [sec("SPAQ", aid(1))])
        [row] = tickers(pg_engine)
        assert row.security_type == "SPAC"

    def test_edgar_fund_verdict_survives_the_refresh(
        self, pg_engine: Engine, calendar: MarketCalendar
    ) -> None:
        with pg_engine.begin() as conn:
            add_ticker(conn, "CEF", "FUND", alpaca_asset_id=aid(1), name="CEF Inc. Common Stock")
        assert refresh(pg_engine, calendar, [sec("CEF", aid(1))]) == TickerRefresh(0, 0, ())
        [row] = tickers(pg_engine)
        assert row.security_type == "FUND"

    def test_mass_vanishing_is_a_vendor_glitch(
        self, pg_engine: Engine, calendar: MarketCalendar
    ) -> None:
        listed = [sec(f"S{i}", aid(i)) for i in range(4)]
        refresh(pg_engine, calendar, listed)
        with pytest.raises(UniverseError, match="4 listed securities vanished"):
            refresh(pg_engine, calendar, [], max_delistings=3)
        assert all(r.active for r in tickers(pg_engine))


def raw(closes: list[float], volume: int) -> list[ScreenBar]:
    return screen_bars(
        [DailyBar("X", date(2026, 10, 1 + i), c, c, c, c, volume) for i, c in enumerate(closes)]
    )


def test_passes_screen() -> None:
    u = RISK.universe  # $10, $10M over 50 sessions; margin 0.5 → $5, $5M
    assert passes_screen(raw([20.0] * 3, 300_000), u, margin=0.5)  # $6M a day
    assert not passes_screen(raw([20.0] * 3, 200_000), u, margin=0.5)  # $4M
    assert not passes_screen(raw([20.0, 20.0, 4.0], 3_000_000), u, margin=0.5)  # last close $4
    assert not passes_screen([], u, margin=0.5)
    assert passes_screen(raw([20.0] * 3, 600_000), u, margin=1.0)
