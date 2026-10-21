"""`scan` (evening chain, after `regime`; README §3, §5.3): the walking skeleton's scan-lite.

    uv run python -m auratrade.jobs.scan --session 2026-10-21 --dry-run

Trend Template rules 1-5 and 8 on session T's universe, a 20-session-high pivot, a 10-session-low
stop capped at 8% below the limit, ranked by RS rating. The best `skeleton.candidates` that
aren't held and have no open mismatch become PENDING signals for the next session;
`execute_entries` takes the first `skeleton.picks` that size to at least one share. Nothing is
written when T's data isn't COMPLETE (fail closed). Tasks 3.2a-3.3 replace this with the real
scans and the ranking.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import Engine

from auratrade.calendar.sessions import load_calendar
from auratrade.config import Settings
from auratrade.db.engine import engine_from_settings
from auratrade.execution import store
from auratrade.execution.signals import expire_signals, load_histories, scan, write_signals
from auratrade.jobs.base import GuardPolicy, JobContext, JobOutput, JobSpec, lambda_handler, run_cli


def make_job(
    *,
    engine_factory: Callable[[Settings], Engine] = engine_from_settings,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> JobSpec:
    def run(ctx: JobContext) -> JobOutput:
        engine = engine_factory(ctx.settings)
        started = time.monotonic()
        with engine.connect() as conn:
            calendar = load_calendar(conn)
            session = calendar.session_for(ctx.session_date)
            if session is None or session.data_status != "COMPLETE":
                status = None if session is None else session.data_status
                ctx.log.warning("scan_skipped", data_status=status)
                return {"signals": 0, "skipped": f"data status {status}"}
            histories = load_histories(conn, calendar, ctx.session_date, ctx.risk)
            held = set(store.open_positions(conn))
            mismatched = {s for _, s in store.open_mismatches(conn) if s}
        loaded = time.monotonic()
        excluded = held | mismatched
        ranked = [c for c in scan(histories, ctx.risk) if c.symbol not in excluded]
        candidates = ranked[: ctx.risk.skeleton.candidates]
        trade_session = calendar.next_session(ctx.session_date).day
        now = clock()
        with engine.begin() as conn:
            expired = expire_signals(conn, before_or_on=ctx.session_date, now=now)
            written = write_signals(
                conn,
                candidates,
                trade_session=trade_session,
                decided_session=ctx.session_date,
                risk_fraction=ctx.risk.skeleton.risk_per_trade,
                now=now,
            )
        return {
            "universe_loaded": len(histories),
            "candidates": [
                {"symbol": c.symbol, "rs": round(c.rs_rating, 1), "trigger": c.levels.trigger}
                for c in candidates
            ],
            "signals": written,
            "trade_session": trade_session.isoformat(),
            "expired": expired,
            "seconds": {
                "load": round(loaded - started, 1),
                "scan": round(time.monotonic() - loaded, 1),
            },
        }

    return JobSpec(name="scan", policy=GuardPolicy.EVENING, run=run)


JOB = make_job()
handler = lambda_handler(JOB)

if __name__ == "__main__":
    sys.exit(run_cli(JOB))
