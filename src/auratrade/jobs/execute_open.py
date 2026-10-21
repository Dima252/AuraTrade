"""`execute_open` (09:20, and the 09:31 check run; README §3, §5.7.3).

    uv run python -m auratrade.jobs.execute_open --session 2026-10-22 --dry-run

Both runs reconcile and enforce the stop invariant under the order lock. Before the open (09:20)
nothing is sold: a stop that can't be placed because the price is already through it leaves the
sale owed. Once the market is open (the 09:31 run) the owed sales go through the sell sequence
(#124: no OPG orders). Which run this is follows from the broker's clock, so a late 09:20 run
behaves as the check run.
"""

from __future__ import annotations

import sys

from auratrade.execution.runtime import (
    DepsFactory,
    enforce_invariant,
    enforce_kill_switch,
    order_section,
    production_deps,
    sell_owed,
)
from auratrade.jobs.base import GuardPolicy, JobContext, JobOutput, JobSpec, lambda_handler, run_cli


def make_job(deps_factory: DepsFactory = production_deps) -> JobSpec:
    def run(ctx: JobContext) -> JobOutput:
        deps = deps_factory(ctx)
        with order_section(ctx, deps) as section:
            invariant = enforce_invariant(section)
            enforce_kill_switch(section)
            sales = sell_owed(section)
            return {
                "market_open": section.desk.market_open,
                "dry_run": section.dry_run,
                **section.rec.details(),
                **invariant.details(),
                "sales": sales,
                "actions": section.desk.actions,
                "alerts": section.alerts,
            }

    return JobSpec(name="execute_open", policy=GuardPolicy.SESSION_TODAY, run=run)


JOB = make_job()
handler = lambda_handler(JOB)

if __name__ == "__main__":
    sys.exit(run_cli(JOB))
