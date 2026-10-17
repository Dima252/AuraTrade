"""Historical backfill (task 1.6, README §5.1, §10): the Parquet history since 2016, then the
last 2 years into Postgres. Free vendor calls only (Alpaca market data and news, SEC EDGAR,
CBOE); no orders.

    uv run python scripts/backfill.py all                 # discover -> bars -> build -> macro
                                                          #   -> news -> edgar (hours; resumable)
    uv run python scripts/backfill.py bars build          # any stages, in this order
    uv run python -m auratrade.jobs.ingest --direct --session <last session>
    uv run python scripts/backfill.py postgres            # after that ingest run

Stages save their progress in `--work` (default var/backfill), so a stopped run picks up where
it left off; delete a stage's folder there to redo it. The history goes to the configured
object store (STORAGE_BACKEND; locally var/storage). The calendar must be synced first
(`python -m auratrade.calendar.sync`).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import structlog

from auratrade.calendar.sessions import MarketCalendar, load_calendar
from auratrade.config import REPO_ROOT, load_risk_config
from auratrade.data.alpaca_data import SIP_DELAY, AlpacaDataClient, alpaca_http
from auratrade.data.edgar import EdgarClient
from auratrade.data.macro import CboeClient
from auratrade.data.nasdaq import NasdaqDirectoryClient
from auratrade.data.news import AlpacaNewsClient
from auratrade.db.engine import engine_from_settings
from auratrade.history.build import build_history, fetch_bars
from auratrade.history.discover import Discovery, discover
from auratrade.history.edgar import edgar_history
from auratrade.history.load import load_postgres
from auratrade.history.macro import macro_history
from auratrade.history.news import fetch_news
from auratrade.jobs.wiring import load_runtime_settings
from auratrade.log import configure_logging
from auratrade.storage import make_store

log = structlog.get_logger("backfill")

STAGES = ("discover", "bars", "build", "macro", "news", "edgar", "postgres")
ALL = STAGES[:-1]


def last_final_session(calendar: MarketCalendar, now: datetime) -> date:
    """The latest session whose SIP bars are final (its close plus the 15-minute delay)."""
    today = now.date()
    session = calendar.session_for(today)
    if session is not None and now >= session.close_at + SIP_DELAY:
        return today
    return calendar.previous_session(today).day


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/backfill.py", description=__doc__.split("\n")[0])
    parser.add_argument("stages", nargs="+", choices=(*STAGES, "all"))
    parser.add_argument("--through", type=date.fromisoformat, help="last session (default: latest)")
    parser.add_argument("--work", type=Path, default=REPO_ROOT / "var" / "backfill")
    args = parser.parse_args(argv)
    wanted = set(ALL) if "all" in args.stages else set(args.stages)

    settings = load_runtime_settings()
    configure_logging(settings.secret_values())
    risk = load_risk_config(settings.risk_config_path)
    if settings.alpaca_api_key is None or settings.alpaca_secret_key is None:
        log.error("backfill_missing_keys", needs="ALPACA_API_KEY / ALPACA_SECRET_KEY")
        return 2
    engine = engine_from_settings(settings)
    store = make_store(settings)
    work: Path = args.work
    work.mkdir(parents=True, exist_ok=True)
    key = settings.alpaca_api_key.get_secret_value()
    secret = settings.alpaca_secret_key.get_secret_value()
    http = alpaca_http(key, secret)
    alpaca = AlpacaDataClient(http)
    cfg = risk.history
    try:
        with engine.connect() as conn:
            calendar = load_calendar(conn)
        through = args.through or last_final_session(calendar, datetime.now(UTC))
        sessions = [s.day for s in calendar.sessions_between(cfg.start, through)]
        log.info("backfill_start", stages=sorted(wanted), through=through.isoformat())
        found: Discovery | None = None
        if wanted & {"discover", "bars", "build"}:
            found = discover(alpaca, NasdaqDirectoryClient(), sessions, risk, work)
            log.info(
                "backfill_discovered",
                assets=len(found.assets),
                swept=len(found.swept),
                symbols=len(found.symbols),
            )
        if "bars" in wanted and found is not None:
            fetched = fetch_bars(alpaca, found.symbols, cfg.start, through, risk, work)
            log.info("backfill_bars_fetched", **vars(fetched))
        if "build" in wanted and found is not None:
            built = build_history(store, work, found.assets, sessions, risk)
            log.info("backfill_built", **built.details())
        if "macro" in wanted:
            rows = macro_history(store, CboeClient(), set(sessions))
            log.info("backfill_macro", rows=rows)
        if "news" in wanted:
            news = fetch_news(
                AlpacaNewsClient(http), store, cfg.start, through, workers=cfg.workers
            )
            log.info(
                "backfill_news",
                months=news.months,
                fetched=news.fetched,
                skipped=news.skipped,
                headlines=news.headlines,
            )
        if "edgar" in wanted:
            if not settings.edgar_user_agent:
                log.error("backfill_missing_keys", needs="EDGAR_USER_AGENT")
                return 2
            edgar = edgar_history(
                store,
                EdgarClient(settings.edgar_user_agent),
                calendar,
                cfg.start,
                through,
                work,
                workers=risk.ingest.http_workers,
            )
            log.info("backfill_edgar", **edgar.details())
        if "postgres" in wanted:
            with engine.begin() as conn:
                loaded = load_postgres(conn, store, calendar, risk, through)
            log.info("backfill_postgres", **loaded.details())
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
