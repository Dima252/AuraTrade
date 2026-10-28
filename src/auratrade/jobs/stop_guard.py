"""`stop_guard` (every 5 minutes, 09:35-16:00; README §3, §5.6.1): the invariant between jobs.

    uv run python -m auratrade.jobs.stop_guard --dry-run

It asks the broker first: every stopped position's live GTC stops cover its full quantity, and
no buy is partly filled (#115); the portfolio's unleveraged funds carry no stop (#155). Plan
levels aren't checked here; the order jobs check them. Only when something is wrong does it
take the order lock without waiting, open the database, reconcile and repair with the plan
levels. While another job holds the lock it skips (a sell sequence may be
between "reduce the stop" and "sell"), and alerts if the lock has been held longer than its
server-side backstop.

It is a quiet job: the runner records it only when it repaired or failed, so the database is
touched only then (README §12, Neon compute).
"""

from __future__ import annotations

import sys

from auratrade.execution import funds
from auratrade.execution.invariant import check, unprotected
from auratrade.execution.runtime import (
    DepsFactory,
    ExecutionDeps,
    enforce_invariant,
    production_deps,
    try_order_section,
)
from auratrade.jobs.base import GuardPolicy, JobContext, JobOutput, JobSpec, lambda_handler, run_cli


def _lock_age_alert(ctx: JobContext, deps: ExecutionDeps) -> str | None:
    holder = deps.lock.holder()
    if holder is None:
        return None
    minutes = holder.held_for.total_seconds() / 60
    limit = ctx.risk.execution.lock_idle_timeout_minutes
    if minutes <= limit:
        return None
    return (
        f"the order lock has been held by {holder.application_name} for {minutes:.0f} min "
        f"(backstop {limit} min) while positions are unprotected"
    )


def make_job(deps_factory: DepsFactory = production_deps) -> JobSpec:
    def run(ctx: JobContext) -> JobOutput:
        deps = deps_factory(ctx)
        if not deps.broker.is_market_open():
            return {"skipped": "market closed"}
        bad = unprotected(
            check(
                deps.broker.positions(),
                deps.broker.open_orders(),
                unstopped=funds.unstopped(ctx.risk.portfolio),
            )
        )
        if not bad:
            return {"ok": True}
        problems = {p.symbol: sorted(x.value for x in p.problems) for p in bad}
        ctx.log.warning("stop_guard_problems", problems=problems)
        with try_order_section(ctx, deps) as section:
            if section is None:
                text = _lock_age_alert(ctx, deps)
                if text is not None:
                    deps.alerter.alert(f"[AuraTrade {ctx.settings.environment}] stop_guard: {text}")
                return {"skipped": "order lock held", "problems": problems}
            report = enforce_invariant(section)
            return {
                "acted": True,
                "dry_run": section.dry_run,
                "problems": problems,
                **report.details(),
                "actions": section.desk.actions,
                "alerts": section.alerts,
            }

    return JobSpec(name="stop_guard", policy=GuardPolicy.MARKET_HOURS, run=run, quiet=True)


JOB = make_job()
handler = lambda_handler(JOB)

if __name__ == "__main__":
    sys.exit(run_cli(JOB))
