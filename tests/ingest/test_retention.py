from __future__ import annotations

import io
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq
from sqlalchemy import Engine, insert, select, text

from auratrade.db.models import DailyBar
from auratrade.history.store import write_bar_session
from auratrade.ingest.retention import run_retention
from auratrade.storage import LocalStore
from tests.ingest.fakes import add_ticker, small_risk, sync_calendar

THU = date(2026, 10, 15)


# A stand-in for the output tables (the real ones need every column filled).
OUTPUT = (("scan_output", "session_date"), ("not_yet_created", "session_date"))


def test_retention(pg_engine: Engine, tmp_path: Path) -> None:
    aaa_id = "00000000-0000-0000-0000-000000000001"
    calendar = sync_calendar(pg_engine)
    days = [s.day for s in calendar.sessions_between(date(2026, 9, 1), THU)]
    with pg_engine.begin() as conn:
        aaa = add_ticker(conn, "AAA", alpaca_asset_id=aaa_id)
        conn.execute(
            insert(DailyBar),
            [
                {
                    "ticker_id": aaa,
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
        conn.execute(text("CREATE TABLE scan_output (session_date date, symbol text)"))
        conn.execute(
            text("INSERT INTO scan_output VALUES ('2026-06-01', 'OLD'), ('2026-10-14', 'NEW')")
        )
    storage = LocalStore(tmp_path)
    # The backfill already wrote the first session, with a security Postgres never held.
    delisted = {
        "session_date": days[0],
        "security": "ZZZ:2016-01-04",
        "symbol": "ZZZ",
        "alpaca_asset_id": None,
        "open": 2.0,
        "high": 2.0,
        "low": 2.0,
        "close": 2.0,
        "volume": 2,
        "raw_close": 2.0,
    }
    write_bar_session(
        storage, days[0], [delisted, delisted | {"security": aaa_id, "symbol": "AAA"}], merge=False
    )
    risk = small_risk(archive_after_sessions=63, db_size_limit_mb=1)
    try:
        with pg_engine.begin() as conn:
            report = run_retention(conn, storage, calendar, risk, THU, output_tables=OUTPUT)
        window = days[-20:]
        assert report.bars_before == window[0]
        assert report.bars_pruned == len(days) - 20
        with pg_engine.connect() as conn:
            kept = (
                conn.execute(select(DailyBar.session_date).order_by(DailyBar.session_date))
                .scalars()
                .all()
            )
            assert kept == window
            left = conn.execute(text("SELECT symbol FROM scan_output")).scalars().all()
        assert left == ["NEW"]
        assert report.archived == {"scan_output": 1}  # a table not created yet is skipped
        assert report.over_alert  # any real database is over a 1 MB limit
        first = days[0]
        archived = pq.read_table(
            io.BytesIO(storage.get(f"parquet/daily_bars/year=2026/{first.isoformat()}.parquet"))
        ).to_pylist()
        assert archived == [
            {
                "session_date": first,
                "security": aaa_id,
                "symbol": "AAA",
                "alpaca_asset_id": aaa_id,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1,
                "raw_close": 1.0,
            },
            delisted,
        ]
        assert storage.list("parquet/scan_output/") == ["parquet/scan_output/2026-10-15.parquet"]
        with pg_engine.begin() as conn:
            again = run_retention(conn, storage, calendar, risk, THU, output_tables=OUTPUT)
        assert (again.bars_pruned, again.archived) == (0, {"scan_output": 0})
    finally:
        with pg_engine.begin() as conn:
            conn.execute(text("DROP TABLE scan_output"))
