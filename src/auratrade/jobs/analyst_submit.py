"""`analyst_submit` (evening chain, after `scan`; README §3, §5.4): a placeholder until task 4.4.

The walking skeleton picks without the LLM, so there is nothing to submit yet. The link ends the
chain cleanly: `scan` invokes it on success.
"""

from __future__ import annotations

import sys

from auratrade.jobs.base import GuardPolicy, JobContext, JobOutput, JobSpec, lambda_handler, run_cli


def run(ctx: JobContext) -> JobOutput:
    ctx.log.info("analyst_submit_placeholder", built_in="task 4.4")
    return {"placeholder": "no Analyst batch until task 4.4"}


JOB = JobSpec(name="analyst_submit", policy=GuardPolicy.EVENING, run=run)
handler = lambda_handler(JOB)

if __name__ == "__main__":
    sys.exit(run_cli(JOB))
