"""`simulate` (evening chain, after `ingest`, before `regime`; README §3, §5.5).

    uv run python -m auratrade.jobs.simulate --session 2026-10-23 --dry-run

**The twin and the book** (task 4.3, #166): the twin, the book's decided targets replayed and
SPY bought and held, all through `sim/portfolio.py` from the trial's first R to T, written whole
to `twin_book`, with the scorecard in each month's `portfolio_journal` row (task 4.4, #167).
Nothing is written before the trial starts, or when T's data isn't COMPLETE (fail closed). The
job places no orders, so `--dry-run` changes nothing here.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable

from sqlalchemy import Engine

from auratrade.calendar.sessions import load_calendar
from auratrade.config import Settings
from auratrade.db.engine import engine_from_settings
from auratrade.jobs.allocate import alpaca
from auratrade.jobs.base import GuardPolicy, JobContext, JobOutput, JobSpec, lambda_handler, run_cli
from auratrade.journal.portfolio import FundBars, simulate_books


def make_job(
    *,
    engine_factory: Callable[[Settings], Engine] = engine_from_settings,
    funds_factory: Callable[[Settings], FundBars] = alpaca,
) -> JobSpec:
    def run(ctx: JobContext) -> JobOutput:
        engine = engine_factory(ctx.settings)
        started = time.monotonic()
        with engine.connect() as conn:
            calendar = load_calendar(conn)
        session = calendar.session_for(ctx.session_date)
        if session is None or session.data_status != "COMPLETE":
            status = None if session is None else session.data_status
            ctx.log.warning("simulate_skipped", data_status=status)
            return {"skipped": f"data status {status}"}
        with engine.begin() as conn:
            books = simulate_books(
                conn, calendar, ctx.risk, funds_factory(ctx.settings), ctx.session_date
            )
        return {"books": books, "seconds": round(time.monotonic() - started, 1)}

    return JobSpec(name="simulate", policy=GuardPolicy.EVENING, run=run)


JOB = make_job()
handler = lambda_handler(JOB)

if __name__ == "__main__":
    sys.exit(run_cli(JOB))
