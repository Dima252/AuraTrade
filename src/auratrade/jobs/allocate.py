"""`allocate` (evening, on R; README §3, §5.3): the rule level, the twin's targets and the
Portfolio Manager's facts for E.

    uv run python -m auratrade.jobs.allocate --session 2026-10-30 --dry-run

On a session that isn't an R it does nothing. On R it runs the twin and the book through R's
close (ranked at R), and writes an ALLOCATED `portfolio_targets` row for E; a row already
DECIDED is left alone. The first R it ever runs on starts the trial (#166). Nothing is written
when R's data isn't COMPLETE (fail closed: no decision on E, which then holds). On `paper` the
drawdown halt is the account's: R turns it off on a DOWN -> UP flip or a new peak (README §7.3)
before the rule level is read; in dry_run the simulated book's halt is used and the flag is left
alone. It places no orders. The last link of the evening chain.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from sqlalchemy import Engine

from auratrade.calendar.sessions import load_calendar
from auratrade.config import Settings, TradingMode
from auratrade.db.engine import engine_from_settings
from auratrade.jobs.base import GuardPolicy, JobContext, JobOutput, JobSpec, lambda_handler, run_cli
from auratrade.journal.portfolio import FundBars, allocate


def alpaca(settings: Settings) -> FundBars:
    from auratrade.data.alpaca_data import AlpacaDataClient, alpaca_http

    if settings.alpaca_api_key is None or settings.alpaca_secret_key is None:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
    return AlpacaDataClient(
        alpaca_http(
            settings.alpaca_api_key.get_secret_value(),
            settings.alpaca_secret_key.get_secret_value(),
        )
    )


def make_job(
    *,
    engine_factory: Callable[[Settings], Engine] = engine_from_settings,
    funds_factory: Callable[[Settings], FundBars] = alpaca,
) -> JobSpec:
    def run(ctx: JobContext) -> JobOutput:
        engine = engine_factory(ctx.settings)
        with engine.connect() as conn:
            calendar = load_calendar(conn)
        session = calendar.session_for(ctx.session_date)
        if session is None or session.data_status != "COMPLETE":
            status = None if session is None else session.data_status
            ctx.log.warning("allocate_skipped", data_status=status)
            return {"skipped": f"data status {status}"}
        with engine.begin() as conn:
            out = allocate(
                conn,
                calendar,
                ctx.risk,
                funds_factory(ctx.settings),
                ctx.session_date,
                live=ctx.settings.trading_mode is TradingMode.PAPER,
                write_halt=not ctx.dry_run,
            )
        ctx.log.info("allocated", **out)
        return out

    return JobSpec(name="allocate", policy=GuardPolicy.EVENING, run=run)


JOB = make_job()
handler = lambda_handler(JOB)

if __name__ == "__main__":
    sys.exit(run_cli(JOB))
