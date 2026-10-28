"""`watchdog` (every 15 minutes, 07:00-22:00 on weekdays; README §3, §5.9).

    uv run python -m auratrade.jobs.watchdog --dry-run

It alerts when something that should have run didn't, which no job can report about itself:
- on a session day, a scheduled heartbeat missing past its grace (`jobs.watchdog_grace_minutes`),
  for the slots `schedule.weekday_slots` expects (`execute_close` on full sessions only);
- a run still RUNNING past `jobs.stale_running_minutes` (its process died: Lambda stops at 15);
- in AWS, a schedule of this environment that isn't enabled (`stop_guard` writes no heartbeat
  unless it acts, so its schedule is checked instead).
Each finding is alerted once per session; the run's details keep it for the next runs.

Every run that completes pings the external dead-man's switch (`HEALTHCHECKS_PING_URL`), so a
watchdog that stops running, or can't reach the database, is noticed outside AWS.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Connection, Engine, or_, select

from auratrade.calendar.sessions import load_calendar
from auratrade.config import Settings
from auratrade.db.engine import engine_from_settings
from auratrade.db.models import JobRunRow
from auratrade.jobs import schedule
from auratrade.jobs.base import GuardPolicy, JobContext, JobOutput, JobSpec, lambda_handler, run_cli
from auratrade.notify.alerts import Alerter

NAME = "watchdog"
PING_TIMEOUT_SECONDS = 10.0

ScheduleStates = Callable[[Settings], list[tuple[str, str]]]


def missing_heartbeats(
    expected: Iterable[schedule.Expected], runs: Iterable[tuple[str, datetime]], now: datetime
) -> list[str]:
    """The expected heartbeats past due without a matching run (job, started_at)."""
    started: dict[str, list[datetime]] = {}
    for job, at in runs:
        started.setdefault(job, []).append(at)
    return [
        f"missing: {e.label}"
        for e in expected
        if e.due <= now
        and not any(e.after is None or at >= e.after for at in started.get(e.job, []))
    ]


def _runs(conn: Connection, day: date) -> list[tuple[str, datetime]]:
    rows = conn.execute(
        select(JobRunRow.job, JobRunRow.started_at).where(JobRunRow.session_date == day)
    ).all()
    return [(job, at) for job, at in rows]


def _stuck(conn: Connection, before: datetime) -> list[str]:
    rows = conn.execute(
        select(JobRunRow.job, JobRunRow.session_date, JobRunRow.run_id).where(
            JobRunRow.status == "RUNNING", JobRunRow.started_at < before
        )
    ).all()
    return [f"stuck: {job} {day} run {run_id}" for job, day, run_id in rows]


def _reported(conn: Connection, ctx: JobContext) -> set[str]:
    rows = conn.execute(
        select(JobRunRow.details).where(
            JobRunRow.job == NAME,
            JobRunRow.session_date == ctx.session_date,
            JobRunRow.run_id != ctx.run_id,
            or_(JobRunRow.status == "SUCCEEDED", JobRunRow.status == "FAILED"),
        )
    ).all()
    return {f for (details,) in rows for f in (details or {}).get("findings", [])}


def aws_schedule_states(settings: Settings) -> list[tuple[str, str]]:
    if settings.schedule_group is None:
        return []
    import boto3

    client = boto3.client("scheduler")
    out = []
    for page in client.get_paginator("list_schedules").paginate(GroupName=settings.schedule_group):
        out += [(s.get("Name", "?"), s.get("State", "?")) for s in page.get("Schedules", [])]
    return out


def http_ping(url: str) -> None:
    import httpx

    httpx.get(url, timeout=PING_TIMEOUT_SECONDS).raise_for_status()


def _alerter(settings: Settings) -> Alerter:
    from auratrade.jobs.wiring import make_alerter

    return make_alerter(settings)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def make_job(
    *,
    engine_factory: Callable[[Settings], Engine] = engine_from_settings,
    alerter_factory: Callable[[Settings], Alerter] = _alerter,
    schedule_states: ScheduleStates = aws_schedule_states,
    ping: Callable[[str], None] = http_ping,
    clock: Callable[[], datetime] = _utc_now,
) -> JobSpec:
    def run(ctx: JobContext) -> JobOutput:
        jobs = ctx.risk.jobs
        now = clock()
        with engine_factory(ctx.settings).connect() as conn:
            session = load_calendar(conn).session_for(ctx.session_date)
            findings: list[str] = []
            if session is not None:
                expected = schedule.expected_heartbeats(
                    schedule.weekday_slots(jobs.chain_wait_minutes),
                    ctx.session_date,
                    early_close=session.is_early_close,
                    grace_minutes=jobs.watchdog_grace_minutes,
                )
                findings += missing_heartbeats(expected, _runs(conn, ctx.session_date), now)
            findings += _stuck(conn, now - timedelta(minutes=jobs.stale_running_minutes))
            reported = _reported(conn, ctx)
        findings += [
            f"schedule {state.lower()}: {name}"
            for name, state in schedule_states(ctx.settings)
            if state != "ENABLED"
        ]
        new = [f for f in findings if f not in reported]
        if new:
            text = "\n".join(f"- {f}" for f in new)
            alerter_factory(ctx.settings).alert(
                f"[AuraTrade {ctx.settings.environment}] watchdog:\n{text}"
            )
        pinged = False
        if ctx.settings.healthchecks_ping_url is not None:
            try:
                ping(ctx.settings.healthchecks_ping_url.get_secret_value())
                pinged = True
            except Exception as exc:
                # The dead-man's switch alerts on its own when the pings stop.
                ctx.log.warning("watchdog_ping_failed", error=type(exc).__name__)
        return {
            "session": session is not None,
            "findings": findings,
            "alerted": new,
            "pinged": pinged,
        }

    return JobSpec(name=NAME, policy=GuardPolicy.WATCHDOG, run=run)


JOB = make_job()
handler = lambda_handler(JOB)

if __name__ == "__main__":
    sys.exit(run_cli(JOB))
