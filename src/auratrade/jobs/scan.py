"""`scan` (evening chain, after `regime`; README §3, §5.3): the full scan, and the walking
skeleton's scan-lite.

    uv run python -m auratrade.jobs.scan --session 2026-10-21 --dry-run

**The skeleton** (until Phase 5): Trend Template rules 1-5 and 8 on session T's universe, a
20-session-high pivot, a 10-session-low stop capped at 8% below the limit, ranked by RS rating.
The best `skeleton.candidates` that aren't held and have no open mismatch become PENDING
signals for the next session; `execute_entries` takes the first `skeleton.picks` that size to at
least one share.

**The full scan** (task 3.7): every strategy scan over the universe, scored, and capped at
`ANALYST_MAX_CANDIDATES` by T's computed regime (``strategies.watchlist``), written to
`daily_watchlist`: the breakout tracker reads every setup, the baseline book the ranked ones.
It runs after the skeleton's signals are committed, so a failure here fails the run (and
alerts) without taking the skeleton's entries away. The Analyst (Phase 4) reads the same list.

Nothing is written when T's data isn't COMPLETE (fail closed).
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import Engine

from auratrade.calendar.sessions import load_calendar
from auratrade.config import Settings
from auratrade.db.engine import engine_from_settings
from auratrade.execution import store
from auratrade.execution.signals import expire_signals, load_histories, scan, write_signals
from auratrade.jobs.base import GuardPolicy, JobContext, JobOutput, JobSpec, lambda_handler, run_cli
from auratrade.strategies.store import load_inputs, write_watchlist
from auratrade.strategies.watchlist import scan_session


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
        skeleton_done = time.monotonic()
        with engine.connect() as conn:
            inputs = load_inputs(conn, calendar, ctx.session_date, ctx.risk)
        inputs_loaded = time.monotonic()
        items = scan_session(
            inputs.histories,
            session=ctx.session_date,
            benchmark=inputs.benchmark,
            regimes=inputs.regimes,
            regime=inputs.regime,
            risk=ctx.risk,
            cap=ctx.settings.analyst_max_candidates,
        )
        scanned = time.monotonic()
        with engine.begin() as conn:
            listed = write_watchlist(
                conn,
                items,
                session=ctx.session_date,
                regime=inputs.regime,
                risk_hash=ctx.risk.config_hash,
            )
        selected = sorted((i for i in items if i.rank is not None), key=lambda i: i.rank or 0)
        return {
            "universe_loaded": len(histories),
            "candidates": [
                {"symbol": c.symbol, "rs": round(c.rs_rating, 1), "trigger": c.levels.trigger}
                for c in candidates
            ],
            "signals": written,
            "trade_session": trade_session.isoformat(),
            "expired": expired,
            "watchlist": {
                "regime": inputs.regime.value,
                "setups": listed,
                "by_strategy": dict(Counter(i.setup.strategy.value for i in items)),
                "ranked": [f"{i.symbol} {i.setup.strategy.value}" for i in selected[:10]],
                "ranked_count": len(selected),
            },
            "seconds": {
                "load": round(loaded - started, 1),
                "scan": round(skeleton_done - loaded, 1),
                "full_load": round(inputs_loaded - skeleton_done, 1),
                "full_scan": round(scanned - inputs_loaded, 1),
                "full_write": round(time.monotonic() - scanned, 1),
            },
        }

    return JobSpec(name="scan", policy=GuardPolicy.EVENING, run=run)


JOB = make_job()
handler = lambda_handler(JOB)

if __name__ == "__main__":
    sys.exit(run_cli(JOB))
