"""The nightly backup (README §10): the kept-forever tables exported to storage by `post_market`.

Neon's free tier keeps a short restore window, and these tables are the validation record, so
each night writes one Parquet file per table: `backup/<table>/<session>.parquet`. S3's lifecycle
keeps 90 days of dailies plus month-ends (task 1.8).
"""

from __future__ import annotations

import io
import json
from collections.abc import Sequence
from datetime import date
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import Connection, MetaData, Table, inspect, select

from auratrade.storage import ObjectStore

BACKUP_PREFIX = "backup"
# The tables README §10 keeps forever: the portfolio's (the validation record) and the order
# layer's. Bars have their own retention (Parquet archives), and job_runs is operational. The
# setup system's tables are no longer written (task 5.2): their last backups stay.
KEPT_FOREVER: tuple[str, ...] = (
    "portfolio_targets",
    "portfolio_journal",
    "llm_decisions",
    "prompt_versions",
    "twin_book",
    "tax_lots",
    "fund_stops",
    "orders",
    "account_snapshots",
    "market_regime",
    "mismatches",
    "system_flags",
)


def _cell(value: Any) -> Any:
    # JSONB columns hold mixed shapes Parquet can't infer one schema for: store them as text.
    return (
        json.dumps(value, sort_keys=True, default=str) if isinstance(value, dict | list) else value
    )


def export_tables(
    conn: Connection,
    storage: ObjectStore,
    session: date,
    tables: Sequence[str] = KEPT_FOREVER,
) -> dict[str, int]:
    """Rows written per table (a table that doesn't exist yet is left out)."""
    inspector = inspect(conn)
    out: dict[str, int] = {}
    for name in tables:
        if not inspector.has_table(name):
            continue
        table = Table(name, MetaData(), autoload_with=conn)
        rows = [{k: _cell(v) for k, v in r._mapping.items()} for r in conn.execute(select(table))]
        schema = None
        if not rows:
            schema = pa.schema([pa.field(c.name, pa.string()) for c in table.columns])
        buffer = io.BytesIO()
        pq.write_table(pa.Table.from_pylist(rows, schema=schema), buffer)
        storage.put(
            f"{BACKUP_PREFIX}/{name}/{session.isoformat()}.parquet",
            buffer.getvalue(),
            content_type="application/vnd.apache.parquet",
        )
        out[name] = len(rows)
    return out
