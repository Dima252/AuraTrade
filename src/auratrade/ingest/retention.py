"""Retention (README §10): keep Neon's free tier under its 0.5 GB.

- `daily_bars` older than the `bar_history_sessions` window move to Parquet, then leave Postgres.
  The portfolio's tables, `llm_decisions` among them, are kept forever: one decision a month is
  the validation record (README §10).
- The database size is reported every evening, with an alert from `db_size_alert_fraction` of
  the limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import Connection

from auratrade.calendar.sessions import MarketCalendar
from auratrade.config import RiskConfig
from auratrade.ingest.bar_store import archive_and_prune_bars, database_size_mb
from auratrade.storage import ObjectStore


@dataclass
class RetentionReport:
    bars_before: date | None = None
    bars_pruned: int = 0
    db_size_mb: float = 0.0
    db_limit_mb: int = 0
    over_alert: bool = False

    def details(self) -> dict[str, object]:
        return {
            "bars_before": self.bars_before.isoformat() if self.bars_before else None,
            "bars_pruned": self.bars_pruned,
            "db_size_mb": round(self.db_size_mb, 1),
            "db_limit_mb": self.db_limit_mb,
            "over_alert": self.over_alert,
        }


def run_retention(
    conn: Connection,
    storage: ObjectStore,
    calendar: MarketCalendar,
    risk: RiskConfig,
    session: date,
) -> RetentionReport:
    cfg = risk.ingest
    report = RetentionReport(db_limit_mb=cfg.db_size_limit_mb)
    report.bars_before = calendar.n_sessions_back(session, cfg.bar_history_sessions - 1).day
    report.bars_pruned = archive_and_prune_bars(conn, storage, report.bars_before)
    report.db_size_mb = database_size_mb(conn)
    report.over_alert = report.db_size_mb >= cfg.db_size_limit_mb * cfg.db_size_alert_fraction
    return report
