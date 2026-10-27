"""`execute_close` (15:40; README §3, §5.6.3, §7): the portfolio's trades.

    uv run python -m auratrade.jobs.execute_close --session 2026-11-02 --dry-run

Under the order lock: reconcile and enforce the stop invariant (the kill switch cancels resting
buys), then
- **the drawdown halt's sale**, on any session while the halt is on and the leveraged fund is
  held: into the market fund (README §7.3)
- **the rebalance** on E, and on its retry (`portfolio.retry_sessions`) while E's run didn't
  finish: toward the decided targets (`execution.portfolio`). Each run is recorded on its
  `portfolio_targets` row; one that finished sets `executed_at`, so a second run that day, or
  the retry, does nothing.

No trades on a half day: its 15:40 is after the close, and E has moved past it. In dry_run the
plan is reported and recorded, and no order is sent.

Until task 5.2 rewrites the schedule this job isn't in it; it runs by hand.
"""

from __future__ import annotations

import sys
from typing import Any

from auratrade.config import TradingMode
from auratrade.db.flags import read_flags
from auratrade.execution.portfolio import due_rebalance, halt_sale, rebalance, record_execution
from auratrade.execution.runtime import (
    DepsFactory,
    enforce_invariant,
    enforce_kill_switch,
    order_section,
    production_deps,
)
from auratrade.jobs.base import GuardPolicy, JobContext, JobOutput, JobSpec, lambda_handler, run_cli


def make_job(deps_factory: DepsFactory = production_deps) -> JobSpec:
    def run(ctx: JobContext) -> JobOutput:
        deps = deps_factory(ctx)
        with order_section(ctx, deps) as section:
            invariant = enforce_invariant(section)
            enforce_kill_switch(section)
            out: dict[str, Any] = {
                "dry_run": section.dry_run,
                "market_open": section.desk.market_open,
                **invariant.details(),
            }
            session = section.calendar.session_for(ctx.session_date)
            if session is not None and session.is_early_close:
                return {**out, "skipped": "half day: no trades", "alerts": section.alerts}
            with deps.engine.connect() as conn:
                halted = read_flags(conn).halt_drawdown
                due = due_rebalance(conn, section.calendar, ctx.session_date, ctx.risk.portfolio)
            if halted:
                out["halt_sale"] = halt_sale(section)
            if due is not None:
                result = rebalance(section, invariant, due)
                finished = bool(result["finished"]) and not section.dry_run
                entry = {
                    "session": ctx.session_date.isoformat(),
                    "run_id": ctx.run_id,
                    "mode": ctx.trading_mode.value,
                    "at": deps.clock().isoformat(),
                    **result,
                }
                with deps.engine.begin() as conn:
                    record_execution(conn, due, entry, finished=finished, at=deps.clock())
                last = due.retry >= ctx.risk.portfolio.retry_sessions
                if not finished and last and ctx.trading_mode is TradingMode.PAPER:
                    section.alert(
                        f"the rebalance of E {due.execution} didn't finish on its last try; "
                        "the book holds where it is until the next E"
                    )
                out["rebalance"] = result
            return {**out, "actions": section.desk.actions, "alerts": section.alerts}

    return JobSpec(name="execute_close", policy=GuardPolicy.SESSION_TODAY, run=run)


JOB = make_job()
handler = lambda_handler(JOB)

if __name__ == "__main__":
    sys.exit(run_cli(JOB))
