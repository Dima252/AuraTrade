"""`simulate` (evening chain, after `ingest`; README §3, §5.12): a placeholder until task 3.7.

The baseline book, the breakout tracker and the verdict scorecard arrive with the simulator
(3.6-3.7). Until then this link only keeps the chain whole, so `regime` and the skeleton's `scan`
run after `ingest` (the chain needs every predecessor to succeed).
"""

from __future__ import annotations

import sys

from auratrade.jobs.base import GuardPolicy, JobContext, JobOutput, JobSpec, lambda_handler, run_cli


def run(ctx: JobContext) -> JobOutput:
    ctx.log.info("simulate_placeholder", built_in="task 3.7")
    return {"placeholder": "nothing to simulate until task 3.7"}


JOB = JobSpec(name="simulate", policy=GuardPolicy.EVENING, run=run)
handler = lambda_handler(JOB)

if __name__ == "__main__":
    sys.exit(run_cli(JOB))
