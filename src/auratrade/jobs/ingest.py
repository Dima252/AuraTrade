"""`ingest` (evening chain, after `post_market`; README §3, §5.1): the data for session T.

    uv run python -m auratrade.jobs.ingest --session 2026-10-15 --dry-run
    uv run python -m auratrade.jobs.ingest --direct --session 2026-10-15

The first form goes through the job runner (calendar guard, heartbeat, chaining: it waits for
`post_market`). The second runs the steps directly, without the runner.

Steps, each in its own transaction, in this order (slimmed to the ETF list in task 5.2; the
stock universe's steps are gone):

1. **Calendar** re-sync and cross-check (a disagreement alerts; README §4).
2. **Bars** for the ETF list: gap-fill, T-1 re-fetch, splits, outliers, coverage →
   `market_sessions.data_status`.
3. **Corporate actions** of the ETF list, 4. **macro** (evening VIX, FRED), 5. **retention** and
   the database size.

Steps 1-2 are what the rest of the chain can't run without: their failure fails the job, and
the chain stops (no increase of exposure next session). Steps 3-5 degrade: a failure is
recorded in the run's details and alerted, the stored data stays as it was, and the chain
continues (the regime has its own staleness rule for macro data). News is fetched on demand by
the Portfolio Manager, not here.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

import structlog
from sqlalchemy import Connection, Engine, text

from auratrade.calendar.sessions import load_calendar
from auratrade.calendar.sources import SessionTimes
from auratrade.calendar.sync import HISTORY_START, alert_text, sync_market_sessions
from auratrade.config import RiskConfig, Settings, load_risk_config
from auratrade.data.alpaca_data import SIP_DELAY, AlpacaDataClient
from auratrade.data.macro import CboeClient, FredClient
from auratrade.db.engine import engine_from_settings
from auratrade.ingest.bar_ingest import INCOMPLETE, BarSource, ingest_bars, tracked_symbols
from auratrade.ingest.corporate_actions import CorporateActionSource, ingest_corporate_actions
from auratrade.ingest.macro import CboeSource, FredSource, ingest_macro
from auratrade.ingest.retention import run_retention
from auratrade.jobs.base import GuardPolicy, JobContext, JobOutput, JobSpec, lambda_handler, run_cli
from auratrade.notify.alerts import Alerter
from auratrade.storage import ObjectStore

log = structlog.get_logger(__name__)

CalendarFetch = Callable[[date, date], list[SessionTimes]]


class IngestError(RuntimeError):
    pass


class AlpacaSource(BarSource, CorporateActionSource, Protocol): ...


@dataclass(frozen=True)
class Sources:
    """The vendors, as the steps use them (the clients in production, fakes in tests)."""

    alpaca: AlpacaSource
    cboe: CboeSource
    calendar_primary: CalendarFetch
    calendar_check: CalendarFetch
    fred: FredSource | None = None


def build_sources(settings: Settings, risk: RiskConfig) -> Sources:
    from auratrade.calendar.sources import AlpacaCalendarClient, exchange_calendar_sessions
    from auratrade.data.alpaca_data import alpaca_http

    if settings.alpaca_api_key is None or settings.alpaca_secret_key is None:
        raise IngestError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
    key = settings.alpaca_api_key.get_secret_value()
    secret = settings.alpaca_secret_key.get_secret_value()
    fred_key = settings.fred_api_key
    return Sources(
        alpaca=AlpacaDataClient(alpaca_http(key, secret), workers=risk.ingest.http_workers),
        cboe=CboeClient(),
        calendar_primary=AlpacaCalendarClient(key, secret).fetch,
        calendar_check=exchange_calendar_sessions,
        fred=FredClient(fred_key.get_secret_value()) if fred_key else None,
    )


@dataclass
class IngestOutcome:
    details: dict[str, Any]
    alerts: list[str]


def run_ingest(
    engine: Engine,
    sources: Sources,
    risk: RiskConfig,
    storage: ObjectStore,
    session: date,
    now: datetime,
) -> IngestOutcome:
    details: dict[str, Any] = {}
    alerts: list[str] = []
    failed: list[str] = []
    # Seconds per step: the job must fit in Lambda's 15 minutes (alert at 10).
    seconds: dict[str, float] = {}
    clock = time.monotonic()

    # 1. Calendar (critical): a re-sync, so a newly announced closure is seen before tonight.
    end = date(session.year + 1, 12, 31)
    synced = sync_market_sessions(
        engine,
        sources.calendar_primary(HISTORY_START, end),
        sources.calendar_check(HISTORY_START, end),
        start=HISTORY_START,
        end=end,
        now=now,
    )
    details["calendar"] = {"inserted": synced.inserted, "updated": synced.updated}
    if (message := alert_text(synced)) is not None:
        alerts.append(message)
    with engine.connect() as conn:
        calendar = load_calendar(conn)
    today = calendar.session_for(session)
    if today is None:
        raise IngestError(f"{session} is no longer a session after the calendar re-sync")
    if now < today.close_at + SIP_DELAY:
        # SIP is 15 minutes delayed: before then session T's bar is still forming.
        raise IngestError(f"{session}'s bars are not final until {today.close_at + SIP_DELAY}")
    details["calendar"] |= {"early_close": today.is_early_close, "minutes": today.minutes}

    seconds["calendar"] = round(time.monotonic() - clock, 1)

    # 2. Bars for the ETF list (critical).
    with _transaction(engine, risk) as conn:
        bars = ingest_bars(conn, sources.alpaca, calendar, risk, session, now)
    details["bars"] = bars.details()
    seconds["bars"] = round(time.monotonic() - clock - sum(seconds.values()), 1)
    if bars.status == INCOMPLETE:
        alerts.append(
            f"ingest {session}: bars INCOMPLETE (no bar for {', '.join(bars.missing_required)}). "
            "No increase of exposure next session."
        )
    elif bars.missing:
        alerts.append(
            f"ingest {session}: no bar for {', '.join(bars.missing)} (halted or delisted?): "
            "no buy of it; held units are marked at the last close."
        )

    # 3-5 degrade.
    def step(name: str, call: Callable[[], Any]) -> Any:
        started = time.monotonic()
        try:
            result = call()
        except Exception as exc:
            failed.append(name)
            details[name] = {"error": f"{type(exc).__name__}: {exc}"}
            log.error("ingest_step_failed", step=name, error=f"{type(exc).__name__}: {exc}")
            return None
        finally:
            seconds[name] = round(time.monotonic() - started, 1)
        details[name] = result.details()
        return result

    def in_transaction[T](call: Callable[[Any], T]) -> Callable[[], T]:
        def run() -> T:
            with _transaction(engine, risk) as conn:
                return call(conn)

        return run

    funds = tracked_symbols(risk)
    step(
        "corporate_actions",
        in_transaction(
            lambda c: ingest_corporate_actions(
                c, sources.alpaca, calendar, risk, session, symbols=funds
            )
        ),
    )
    macro = step(
        "macro",
        in_transaction(
            lambda c: ingest_macro(c, sources.cboe, sources.fred, calendar, risk, session)
        ),
    )
    if macro is not None and macro.missing:
        alerts.append(
            f"ingest {session}: no value for {', '.join(macro.missing)} tonight "
            "(CBOE quote not final, history file and FRED behind). The regime uses the latest."
        )
    retention = step(
        "retention",
        in_transaction(lambda c: run_retention(c, storage, calendar, risk, session)),
    )
    if retention is not None and retention.over_alert:
        alerts.append(
            f"database at {retention.db_size_mb:.0f} MB of {retention.db_limit_mb} MB "
            f"(alert from {risk.ingest.db_size_alert_fraction:.0%})"
        )
    if failed:
        alerts.append(f"ingest {session}: steps failed, data left as it was: {', '.join(failed)}")
    details["failed_steps"] = failed
    details["seconds"] = seconds
    return IngestOutcome(details, alerts)


@contextmanager
def _transaction(engine: Engine, risk: RiskConfig) -> Iterator[Connection]:
    """A step's transaction, allowed to sit idle while its vendor calls run."""
    with engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('idle_in_transaction_session_timeout', :limit, true)"),
            {"limit": f"{risk.ingest.transaction_idle_minutes}min"},
        )
        yield conn


def _alerter(settings: Settings) -> Alerter:
    from auratrade.jobs.wiring import make_alerter

    return make_alerter(settings)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def make_job(
    *,
    engine_factory: Callable[[Settings], Engine] = engine_from_settings,
    sources_factory: Callable[[Settings, RiskConfig], Sources] = build_sources,
    alerter_factory: Callable[[Settings], Alerter] = _alerter,
    clock: Callable[[], datetime] = _utc_now,
) -> JobSpec:
    def run(ctx: JobContext) -> JobOutput:
        outcome = run_ingest(
            engine_factory(ctx.settings),
            sources_factory(ctx.settings, ctx.risk),
            ctx.risk,
            ctx.storage,
            ctx.session_date,
            clock(),
        )
        if outcome.alerts:
            alerter = alerter_factory(ctx.settings)
            for message in outcome.alerts:
                alerter.alert(f"[AuraTrade {ctx.settings.environment}] {message}")
        ctx.log.info("ingest_done", **{k: v for k, v in outcome.details.items() if k != "bars"})
        return outcome.details

    return JobSpec(name="ingest", policy=GuardPolicy.EVENING, run=run)


JOB = make_job()
handler = lambda_handler(JOB)


def direct_main(argv: Sequence[str]) -> int:
    """Run the steps outside the job runner: no heartbeat, no chain check, no alerts (the
    output is logged). By hand."""
    from auratrade.jobs.wiring import load_runtime_settings
    from auratrade.log import configure_logging
    from auratrade.storage import make_store

    parser = argparse.ArgumentParser(prog="python -m auratrade.jobs.ingest --direct")
    parser.add_argument("--session", type=date.fromisoformat, required=True)
    args = parser.parse_args(argv)
    settings = load_runtime_settings()
    configure_logging(settings.secret_values())
    risk = load_risk_config(settings.risk_config_path)
    engine = engine_from_settings(settings)
    try:
        outcome = run_ingest(
            engine,
            build_sources(settings, risk),
            risk,
            make_store(settings),
            args.session,
            _utc_now(),
        )
    finally:
        engine.dispose()
    log.info("ingest_direct_done", session=args.session.isoformat(), **outcome.details)
    for message in outcome.alerts:
        log.warning("ingest_alert", alert=message)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--direct", action="store_true")
    known, rest = pre.parse_known_args(argv)
    return direct_main(rest) if known.direct else run_cli(JOB, rest)


if __name__ == "__main__":
    sys.exit(main())
