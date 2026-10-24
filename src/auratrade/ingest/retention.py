"""Retention (README §10): keep Neon's free tier under its 0.5 GB.

- `daily_bars` older than the `bar_history_sessions` window move to Parquet, then leave Postgres.
- Scanner, tracker and LLM output rows older than `archive_after_sessions` move to Parquet,
  one object per table and run. `llm_decisions` arrives in Phase 4; until then it is
  skipped.
- The database size is reported every evening, with an alert from `db_size_alert_fraction` of
  the limit.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import Connection, MetaData, Table, delete, inspect, select

from auratrade.calendar.sessions import MarketCalendar
from auratrade.config import RiskConfig
from auratrade.ingest.bar_store import archive_and_prune_bars, database_size_mb
from auratrade.storage import ObjectStore

# (table, date column) of the output kept 90 days in Postgres (README §10).
OUTPUT_TABLES: tuple[tuple[str, str], ...] = (
    ("daily_watchlist", "session_date"),
    ("breakout_tracker", "trigger_session"),
    ("llm_decisions", "session_date"),
)
ARCHIVE_PREFIX = "parquet"


@dataclass
class RetentionReport:
    bars_before: date | None = None
    bars_pruned: int = 0
    archived: dict[str, int] = field(default_factory=dict)
    db_size_mb: float = 0.0
    db_limit_mb: int = 0
    over_alert: bool = False

    def details(self) -> dict[str, object]:
        return {
            "bars_before": self.bars_before.isoformat() if self.bars_before else None,
            "bars_pruned": self.bars_pruned,
            "archived": self.archived,
            "db_size_mb": round(self.db_size_mb, 1),
            "db_limit_mb": self.db_limit_mb,
            "over_alert": self.over_alert,
        }


def archive_rows(
    conn: Connection,
    storage: ObjectStore,
    table_name: str,
    column: str,
    before: date,
    session: date,
) -> int | None:
    """Move rows with `column` < `before` to `parquet/<table>/<session>.parquet`, then delete
    them. None when the table doesn't exist yet."""
    if not inspect(conn).has_table(table_name):
        return None
    table = Table(table_name, MetaData(), autoload_with=conn)
    rows = [dict(r._mapping) for r in conn.execute(select(table).where(table.c[column] < before))]
    if not rows:
        return 0
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), buffer)
    key = f"{ARCHIVE_PREFIX}/{table_name}/{session.isoformat()}.parquet"
    storage.put(key, buffer.getvalue(), content_type="application/vnd.apache.parquet")
    conn.execute(delete(table).where(table.c[column] < before))
    return len(rows)


def run_retention(
    conn: Connection,
    storage: ObjectStore,
    calendar: MarketCalendar,
    risk: RiskConfig,
    session: date,
    *,
    output_tables: Sequence[tuple[str, str]] = OUTPUT_TABLES,
) -> RetentionReport:
    cfg = risk.ingest
    report = RetentionReport(db_limit_mb=cfg.db_size_limit_mb)
    report.bars_before = calendar.n_sessions_back(session, cfg.bar_history_sessions - 1).day
    report.bars_pruned = archive_and_prune_bars(conn, storage, report.bars_before)
    output_before = calendar.n_sessions_back(session, cfg.archive_after_sessions).day
    for table_name, column in output_tables:
        moved = archive_rows(conn, storage, table_name, column, output_before, session)
        if moved is not None:
            report.archived[table_name] = moved
    report.db_size_mb = database_size_mb(conn)
    report.over_alert = report.db_size_mb >= cfg.db_size_limit_mb * cfg.db_size_alert_fraction
    return report
