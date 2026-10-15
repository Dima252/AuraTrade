"""`daily_bars` I/O for ingest. Every write is an upsert that changes nothing when the values
are equal, so a second run of the same evening writes no rows (task 1.5 acceptance)."""

from __future__ import annotations

import io
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import Connection, delete, func, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert

from auratrade.db.models import BarCorrection, DailyBar, MarketSession, Ticker
from auratrade.ingest.bars import Correction, IngestBar
from auratrade.storage import ObjectStore

WRITE_CHUNK = 2000
_VALUES = ("open", "high", "low", "close", "volume", "raw_close")
ARCHIVE_PREFIX = "parquet/daily_bars"


def stored_tail(conn: Connection, ticker_ids: Iterable[int], n: int) -> dict[int, list[IngestBar]]:
    """The last `n` stored bars of each ticker, oldest first."""
    ids = list(ticker_ids)
    if not ids:
        return {}
    ranked = (
        select(
            DailyBar,
            func.row_number()
            .over(partition_by=DailyBar.ticker_id, order_by=DailyBar.session_date.desc())
            .label("rank"),
        )
        .where(DailyBar.ticker_id.in_(ids))
        .subquery()
    )
    rows = conn.execute(select(ranked).where(ranked.c.rank <= n)).all()
    out: dict[int, list[IngestBar]] = {}
    for r in sorted(rows, key=lambda r: (r.ticker_id, r.session_date)):
        out.setdefault(r.ticker_id, []).append(
            IngestBar(r.session_date, r.open, r.high, r.low, r.close, r.volume, r.raw_close)
        )
    return out


def _rows(bars: Mapping[int, Sequence[IngestBar]]) -> list[dict[str, object]]:
    return [
        {
            "ticker_id": ticker_id,
            "session_date": b.session,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
            "raw_close": b.raw_close,
        }
        for ticker_id, series in bars.items()
        for b in series
    ]


def upsert_bars(conn: Connection, bars: Mapping[int, Sequence[IngestBar]]) -> int:
    """Insert or update; returns the rows that actually changed."""
    rows = _rows(bars)
    written = 0
    for i in range(0, len(rows), WRITE_CHUNK):
        stmt = insert(DailyBar).values(rows[i : i + WRITE_CHUNK])
        new = stmt.excluded
        current = tuple_(*(getattr(DailyBar, c) for c in _VALUES))
        incoming = tuple_(*(getattr(new, c) for c in _VALUES))
        stmt = stmt.on_conflict_do_update(
            index_elements=["ticker_id", "session_date"],
            set_={c: getattr(new, c) for c in _VALUES},
            where=current.is_distinct_from(incoming),
        )
        # RETURNING counts the rows inserted or changed (rowcount isn't reliable here).
        written += len(conn.execute(stmt.returning(DailyBar.ticker_id)).all())
    return written


def replace_window(conn: Connection, ticker_id: int, bars: Sequence[IngestBar]) -> int:
    """Swap a ticker's stored bars for a freshly fetched window (after a split)."""
    conn.execute(delete(DailyBar).where(DailyBar.ticker_id == ticker_id))
    return upsert_bars(conn, {ticker_id: bars})


def record_corrections(
    conn: Connection, corrections: Mapping[int, Sequence[Correction]], now: datetime
) -> int:
    rows = [
        {
            "ticker_id": ticker_id,
            "session_date": c.session,
            "field": c.field.value,
            "original": c.original,
            "corrected": c.corrected,
            "detected_at": now,
        }
        for ticker_id, found in corrections.items()
        for c in found
    ]
    if not rows:
        return 0
    stmt = insert(BarCorrection).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["ticker_id", "session_date", "field"],
        set_={"original": stmt.excluded.original, "corrected": stmt.excluded.corrected},
        where=tuple_(BarCorrection.original, BarCorrection.corrected).is_distinct_from(
            tuple_(stmt.excluded.original, stmt.excluded.corrected)
        ),
    )
    return len(conn.execute(stmt.returning(BarCorrection.ticker_id)).all())


def set_data_status(
    conn: Connection, session: date, status: str, coverage: float, now: datetime
) -> None:
    conn.execute(
        update(MarketSession)
        .where(MarketSession.session_date == session)
        .values(data_status=status, coverage=coverage, data_checked_at=now)
    )


# --- retention ---------------------------------------------------------------------------


def archive_and_prune_bars(conn: Connection, storage: ObjectStore, before: date) -> int:
    """Move bars older than `before` to Parquet (one object per session, keyed by symbol and
    asset id, since ticker ids are per database), then delete them."""
    rows = conn.execute(
        select(
            DailyBar.session_date,
            Ticker.symbol,
            Ticker.alpaca_asset_id,
            *(getattr(DailyBar, c) for c in _VALUES),
        )
        .join(Ticker, Ticker.id == DailyBar.ticker_id)
        .where(DailyBar.session_date < before)
        .order_by(DailyBar.session_date, Ticker.symbol)
    ).all()
    if not rows:
        return 0
    by_session: dict[date, list[dict[str, object]]] = {}
    for r in rows:
        by_session.setdefault(r.session_date, []).append(dict(r._mapping))
    for session, records in by_session.items():
        key = f"{ARCHIVE_PREFIX}/year={session.year}/{session.isoformat()}.parquet"
        storage.put(key, _parquet(records), content_type="application/vnd.apache.parquet")
    result = conn.execute(delete(DailyBar).where(DailyBar.session_date < before))
    return int(result.rowcount)


def _parquet(records: Sequence[Mapping[str, object]]) -> bytes:
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(list(records)), buffer)
    return buffer.getvalue()


def database_size_mb(conn: Connection) -> float:
    size = conn.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
    return float(size) / (1024 * 1024)
