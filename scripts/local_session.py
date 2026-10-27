"""Run the portfolio's jobs on this PC at their scheduled times (tasks 1.7, 5.2).

    TRADING_MODE=paper uv run python scripts/local_session.py --until 2026-10-26

A stand-in for EventBridge, from the same schedule as the AWS stack (`jobs.schedule`), all
America/New_York on weekdays: `portfolio_manager` 08:45 (a paid Claude call on E),
`execute_open` 09:20 and 09:31, `stop_guard` every 5 minutes 09:35-16:00, `execute_close` 15:40,
and `post_market --chain` at 16:20, which runs the rest of the evening chain in this process, so
the chain's fallback slots and the watchdog are left out.

Each job is its own process, run one after another, so a slow job delays the next rather than
overlapping it, and a `stop_guard` slot at the same minute as another job is dropped; the jobs'
own calendar guards skip holidays. Runs already past when the script starts are skipped. The
trading mode comes from the environment, as for any job.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

from auratrade.config import load_risk_config
from auratrade.jobs import schedule
from auratrade.jobs.schedule import NY

REPO_ROOT = Path(__file__).resolve().parents[1]
# Locally post_market runs the evening chain in-process, and there's no dead-man's switch.
LEFT_OUT = frozenset({"watchdog"})

Run = tuple[datetime, str, tuple[str, ...]]


def day_plan(day: date) -> list[Run]:
    if day.weekday() >= 5:
        return []
    head = schedule.EVENING_CHAIN[0].job
    chain_wait = load_risk_config().jobs.chain_wait_minutes
    runs: list[Run] = []
    for slot in schedule.weekday_slots(chain_wait):
        if slot.job in LEFT_OUT or schedule.predecessor(slot.job) is not None:
            continue
        extra = ("--chain",) if slot.job == head else ()
        runs += [(datetime.combine(day, t, tzinfo=NY), slot.job, extra) for t in slot.times()]
    taken = {at for at, job, _ in runs if job != "stop_guard"}
    runs = [r for r in runs if r[1] != "stop_guard" or r[0] not in taken]
    return sorted(runs, key=lambda r: r[0])


def plan(first: date, until: date) -> list[Run]:
    out: list[Run] = []
    day = first
    while day <= until:
        out += day_plan(day)
        day += timedelta(days=1)
    return out


def log(text: str) -> None:
    stamp = datetime.now(NY).strftime("%Y-%m-%d %H:%M:%S")
    sys.stdout.write(f"{stamp} ET  {text}\n")
    sys.stdout.flush()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--until", type=date.fromisoformat, required=True, help="last day (ET)")
    args = parser.parse_args(argv)
    runs = [r for r in plan(datetime.now(NY).date(), args.until) if r[0] > datetime.now(NY)]
    log(f"{len(runs)} runs planned through {args.until}")
    failures = 0
    for at, job, extra in runs:
        wait = (at - datetime.now(NY)).total_seconds()
        if wait > 0:
            time.sleep(wait)
        started = time.monotonic()
        command = [sys.executable, "-m", f"auratrade.jobs.{job}", *extra]
        result = subprocess.run(  # noqa: S603 - our own module, fixed arguments
            command, cwd=REPO_ROOT, capture_output=True, text=True, check=False
        )
        failures += result.returncode != 0
        seconds = time.monotonic() - started
        log(f"{job} {' '.join(extra)} exit {result.returncode} in {seconds:.0f} s")
        if result.returncode != 0:
            log((result.stdout + result.stderr)[-2000:])
    log(f"done: {failures} failed runs")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
