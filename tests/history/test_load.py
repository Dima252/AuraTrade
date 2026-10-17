from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from sqlalchemy import Engine, select

from auratrade.db.models import EarningsEvent, Fundamental, MacroObservation, Ticker
from auratrade.history.build import SECURITIES, SECURITY_SCHEMA
from auratrade.history.edgar import EARNINGS, EARNINGS_SCHEMA, FUNDAMENTALS, FUNDAMENTALS_SCHEMA
from auratrade.history.load import load_postgres
from auratrade.history.macro import MACRO, MACRO_SCHEMA
from auratrade.history.store import put_table, table_key
from auratrade.storage import LocalStore
from tests.ingest.fakes import add_ticker, small_risk, sync_calendar

THU = date(2026, 10, 15)
AAA, BBB = "00000000-0000-0000-0000-00000000000a", "00000000-0000-0000-0000-00000000000b"


def securities() -> list[dict[str, Any]]:
    base = {
        "name": None,
        "exchange": None,
        "security_type": "COMMON",
        "type_assumed": False,
        "source": "ACTIVE",
        "first_session": date(2026, 9, 1),
        "last_session": THU,
        "active": True,
        "bars": 30,
        "relisted": False,
        "cik": None,
        "sic": None,
    }
    return [
        base | {"security": AAA, "symbol": "AAA", "alpaca_asset_id": AAA, "listed_at": None},
        base
        | {"security": BBB, "symbol": "BBB", "alpaca_asset_id": BBB, "listed_at": date(2026, 9, 1)},
        base | {"security": "OLD:2016-01-04", "symbol": "OLD", "alpaca_asset_id": None},
    ]


def event(security: str, report: date) -> dict[str, Any]:
    at = datetime.combine(report, datetime.min.time(), tzinfo=UTC).replace(hour=21)
    return {
        "security": security,
        "symbol": "AAA",
        "cik": 1,
        "report_date": report,
        "timing": "AFTER_CLOSE",
        "accepted_at": at,
        "headline_at": None,
        "accession": f"e-{report}",
    }


def quarter(end: date, filed: date) -> dict[str, Any]:
    return {
        "security": AAA,
        "cik": 1,
        "period_end": end,
        "fiscal_year": end.year,
        "fiscal_quarter": (end.month - 1) // 3 + 1,
        "filed_date": filed,
        "form": "10-Q",
        "accession": f"q-{end}",
        "eps_diluted": 1.0,
        "revenue": 100,
        "eps_growth": None,
        "revenue_growth": None,
        "eps_acceleration": None,
        "revenue_acceleration": None,
    }


def history(tmp_path: Path, window_start: date) -> LocalStore:
    store = LocalStore(tmp_path)
    put_table(store, table_key(SECURITIES), pa.Table.from_pylist(securities(), SECURITY_SCHEMA))
    events = [event(AAA, date(2026, 7, 28)), event(AAA, date(2026, 10, 13))]
    events.append(event("OLD:2016-01-04", date(2026, 10, 13)))  # no Postgres ticker
    put_table(store, table_key(EARNINGS), pa.Table.from_pylist(events, EARNINGS_SCHEMA))
    quarters = [
        quarter(date(2025, 12, 31), date(2026, 2, 5)),
        quarter(date(2026, 3, 31), date(2026, 5, 5)),
        quarter(date(2026, 6, 30), date(2026, 8, 5)),
    ]
    put_table(store, table_key(FUNDAMENTALS), pa.Table.from_pylist(quarters, FUNDAMENTALS_SCHEMA))
    macro = [
        {"series": "VIX", "observation_date": d, "value": 15.0, "source": "CBOE_HISTORY"}
        for d in (date(2026, 1, 5), window_start, THU)
    ]
    put_table(store, table_key(MACRO), pa.Table.from_pylist(macro, MACRO_SCHEMA))
    return store


def test_load_postgres(pg_engine: Engine, tmp_path: Path) -> None:
    calendar = sync_calendar(pg_engine)
    risk = small_risk(fundamentals_quarters=2)  # a 20-session window
    window_start = calendar.n_sessions_back(THU, 19).day
    store = history(tmp_path, window_start)
    with pg_engine.begin() as conn:
        aaa = add_ticker(conn, "AAA", alpaca_asset_id=AAA)
        bbb = add_ticker(conn, "BBB", alpaca_asset_id=BBB, listed_at=date(2026, 9, 2))
        add_ticker(conn, "CCC", alpaca_asset_id="00000000-0000-0000-0000-00000000000c")
        report = load_postgres(conn, store, calendar, risk, THU)
    assert report.details() == {
        "window_start": window_start.isoformat(),
        "tickers_matched": 2,
        "listed_at_set": 0,  # BBB already had one; AAA's is unknown
        "earnings_events": 1,  # the July report is before the 20-session window
        "fundamentals": 2,
        "macro": 2,
    }
    with pg_engine.connect() as conn:
        events = conn.execute(select(EarningsEvent.ticker_id, EarningsEvent.report_date)).all()
        assert [tuple(e) for e in events] == [(aaa, date(2026, 10, 13))]
        ends = conn.execute(select(Fundamental.period_end).order_by(Fundamental.period_end))
        assert list(ends.scalars()) == [date(2026, 3, 31), date(2026, 6, 30)]
        days = conn.execute(select(MacroObservation.observation_date).order_by("observation_date"))
        assert list(days.scalars()) == [window_start, THU]
        listed = conn.execute(select(Ticker.listed_at).where(Ticker.id == bbb)).scalar_one()
        assert listed == date(2026, 9, 2)

    # Again: nothing new, and a ticker without a listing date gets the history's.
    with pg_engine.begin() as conn:
        conn.execute(Ticker.__table__.update().where(Ticker.id == bbb).values(listed_at=None))
        again = load_postgres(conn, store, calendar, risk, THU)
    assert (again.earnings_events, again.fundamentals, again.macro, again.listed_at_set) == (
        0,
        0,
        0,
        1,
    )


def test_load_needs_the_history(pg_engine: Engine, tmp_path: Path) -> None:
    calendar = sync_calendar(pg_engine)
    with pg_engine.begin() as conn, pytest.raises(FileNotFoundError):
        load_postgres(conn, LocalStore(tmp_path), calendar, small_risk(), THU)
