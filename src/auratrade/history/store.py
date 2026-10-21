"""The Parquet history in the object store (README §5.1, §10): its layout, writes and reads.

- **Bars:** `parquet/daily_bars/year=YYYY/YYYY-MM-DD.parquet`, one object per session, keyed by
  `security` (an Alpaca asset id, or the symbol and first session for a security no asset
  names; `securities`). The backfill writes 2016 onward; the evening retention adds each
  session that leaves Postgres's window, merging into the backfill's object so the securities
  Postgres never held (delisted before the database existed) stay.
- **Tables:** `parquet/history/<name>.parquet` (securities, bar corrections, earnings events,
  fundamentals, macro), the universe membership per year and the news headlines per month.

Ticker ids differ per database, so nothing here uses them.
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from auratrade.storage import ObjectStore

BARS_PREFIX = "parquet/daily_bars"
TABLES_PREFIX = "parquet/history"
PARQUET = "application/vnd.apache.parquet"

BAR_SCHEMA = pa.schema(
    [
        ("session_date", pa.date32()),
        ("security", pa.string()),
        ("symbol", pa.string()),
        ("alpaca_asset_id", pa.string()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.int64()),
        ("raw_close", pa.float64()),
    ]
)
BAR_COLUMNS = tuple(BAR_SCHEMA.names)


def bar_key(session: date) -> str:
    return f"{BARS_PREFIX}/year={session.year}/{session.isoformat()}.parquet"


def table_key(name: str) -> str:
    return f"{TABLES_PREFIX}/{name}.parquet"


def membership_key(year: int) -> str:
    return f"{TABLES_PREFIX}/universe/year={year}.parquet"


def news_key(year: int, month: int) -> str:
    return f"{TABLES_PREFIX}/news/{year:04d}-{month:02d}.parquet"


def to_bytes(table: pa.Table) -> bytes:
    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    return buffer.getvalue()


def from_bytes(data: bytes) -> pa.Table:
    return pq.read_table(io.BytesIO(data))


def put_table(store: ObjectStore, key: str, table: pa.Table) -> None:
    store.put(key, to_bytes(table), content_type=PARQUET)


def get_table(store: ObjectStore, key: str) -> pa.Table | None:
    return from_bytes(store.get(key)) if store.exists(key) else None


# --- bars --------------------------------------------------------------------------------


def write_bar_session(
    store: ObjectStore, session: date, rows: Sequence[Mapping[str, Any]], *, merge: bool
) -> int:
    """Write one session's bars; with `merge`, the securities already stored for that session
    and absent from `rows` are kept (`rows` win for the others). Returns the rows written."""
    table = pa.Table.from_pylist([{c: r.get(c) for c in BAR_COLUMNS} for r in rows], BAR_SCHEMA)
    return write_bar_table(store, session, table, merge=merge)


def write_bar_table(store: ObjectStore, session: date, table: pa.Table, *, merge: bool) -> int:
    """As `write_bar_session`, for a table in `BAR_SCHEMA`."""
    key = bar_key(session)
    if merge and (old := get_table(store, key)) is not None:
        old = old.select(list(BAR_COLUMNS)).cast(BAR_SCHEMA)
        kept = old.filter(pc.invert(pc.is_in(old["security"], pc.unique(table["security"]))))
        table = pa.concat_tables([kept, table])
    table = table.sort_by([("symbol", "ascending"), ("security", "ascending")])
    put_table(store, key, table)
    return int(table.num_rows)


def bar_sessions(store: ObjectStore) -> list[date]:
    """The sessions with a bars object, oldest first."""
    out = []
    for key in store.list(f"{BARS_PREFIX}/"):
        name = key.rsplit("/", 1)[-1]
        if name.endswith(".parquet"):
            out.append(date.fromisoformat(name.removesuffix(".parquet")))
    return sorted(out)


def read_bars(
    store: ObjectStore,
    start: date,
    end: date,
    *,
    securities: Iterable[str] | None = None,
) -> pa.Table:
    """Bars of sessions in [start, end], ordered by security and session."""
    wanted = None if securities is None else pa.array(sorted(set(securities)), pa.string())
    parts = []
    for session in bar_sessions(store):
        if not start <= session <= end:
            continue
        table = from_bytes(store.get(bar_key(session))).select(list(BAR_COLUMNS))
        table = table.cast(BAR_SCHEMA)
        if wanted is not None:
            table = table.filter(pc.is_in(table["security"], wanted))
        parts.append(table)
    if not parts:
        return BAR_SCHEMA.empty_table()
    return pa.concat_tables(parts).sort_by(
        [("security", "ascending"), ("session_date", "ascending")]
    )
