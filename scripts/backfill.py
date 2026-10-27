"""Historical backfill for the portfolio's backtest (task 3.18, README §5.1): free vendor calls
only (Kenneth French's library, Robert Shiller's dividends, Alpaca market data); no orders.

    uv run python scripts/backfill.py longhistory         # the long history and SPY/SSO/BIL

The history goes to the configured object store (STORAGE_BACKEND; locally var/storage). The
calendar must be synced first (`python -m auratrade.calendar.sync`). The stock universe's
history since 2016 (the setup system's and the momentum book's stages) went in task 5.2 (#161);
the Parquet files it wrote stay in storage.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime

import structlog

from auratrade.calendar.sessions import MarketCalendar, load_calendar
from auratrade.config import load_risk_config
from auratrade.data.alpaca_data import SIP_DELAY, AlpacaDataClient, alpaca_http
from auratrade.data.french import FrenchClient
from auratrade.data.shiller import ShillerClient
from auratrade.db.engine import engine_from_settings
from auratrade.history.benchmarks import (
    PORTFOLIO_FUND_SYMBOLS,
    PORTFOLIO_FUNDS,
    benchmark_history,
)
from auratrade.history.long_history import long_history
from auratrade.jobs.wiring import load_runtime_settings
from auratrade.log import configure_logging
from auratrade.storage import make_store

log = structlog.get_logger("backfill")

STAGES = ("longhistory",)


def last_final_session(calendar: MarketCalendar, now: datetime) -> date:
    """The latest session whose SIP bars are final (its close plus the 15-minute delay)."""
    today = now.date()
    session = calendar.session_for(today)
    if session is not None and now >= session.close_at + SIP_DELAY:
        return today
    return calendar.previous_session(today).day


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/backfill.py", description=__doc__.split("\n")[0])
    parser.add_argument("stages", nargs="+", choices=STAGES)
    parser.add_argument("--through", type=date.fromisoformat, help="last session (default: latest)")
    args = parser.parse_args(argv)

    settings = load_runtime_settings()
    configure_logging(settings.secret_values())
    risk = load_risk_config(settings.risk_config_path)
    if settings.alpaca_api_key is None or settings.alpaca_secret_key is None:
        log.error("backfill_missing_keys", needs="ALPACA_API_KEY / ALPACA_SECRET_KEY")
        return 2
    engine = engine_from_settings(settings)
    store = make_store(settings)
    http = alpaca_http(
        settings.alpaca_api_key.get_secret_value(), settings.alpaca_secret_key.get_secret_value()
    )
    cfg = risk.history
    alpaca = AlpacaDataClient(http, workers=cfg.workers)
    try:
        with engine.connect() as conn:
            calendar = load_calendar(conn)
        through = args.through or last_final_session(calendar, datetime.now(UTC))
        sessions = [s.day for s in calendar.sessions_between(cfg.start, through)]
        log.info("backfill_start", stages=sorted(args.stages), through=through.isoformat())
        stored = long_history(store, FrenchClient(), ShillerClient(), datetime.now(UTC))
        log.info("backfill_long_history", **stored.details())
        funds = benchmark_history(
            store,
            alpaca,
            cfg.start,
            through,
            set(sessions),
            symbols=PORTFOLIO_FUND_SYMBOLS,
            name=PORTFOLIO_FUNDS,
        )
        log.info("backfill_portfolio_funds", **funds)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
