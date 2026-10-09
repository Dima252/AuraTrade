"""The Postgres `JobRunStore` (`job_runs` table)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Engine, exists, func, insert, select, text, update
from sqlalchemy.orm import Session

from auratrade.db.models import JobRunRow
from auratrade.jobs.runs import JobRun, JobStatus

# First key of the two-key advisory lock; other subsystems (e.g. the order lock) use
# their own, so the keyspaces can't collide.
JOB_RUNS_LOCK_CLASS = 1


class SqlJobRunStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def try_start(self, run: JobRun, *, exclusive: bool, stale_before: datetime) -> bool:
        with self._engine.begin() as conn:
            if exclusive:
                # Serialises claims for this job and session until the transaction ends.
                conn.execute(
                    text("SELECT pg_advisory_xact_lock(CAST(:cls AS int4), hashtext(:key))"),
                    {"cls": JOB_RUNS_LOCK_CLASS, "key": f"{run.job}/{run.session_date}"},
                )
                t = JobRunRow
                blocking = select(
                    exists().where(
                        t.job == run.job,
                        t.session_date == run.session_date,
                        (t.status == JobStatus.SUCCEEDED.value)
                        | ((t.status == JobStatus.RUNNING.value) & (t.started_at > stale_before)),
                    )
                )
                if conn.execute(blocking).scalar_one():
                    return False
            conn.execute(insert(JobRunRow).values(**_row(run)))
        return True

    def finish(
        self,
        run_id: str,
        status: JobStatus,
        finished_at: datetime,
        *,
        details: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "status": status.value,
            "finished_at": finished_at,
            "error": error,
        }
        if details is not None:
            values["details"] = details
        with self._engine.begin() as conn:
            result = conn.execute(
                update(JobRunRow).where(JobRunRow.run_id == run_id).values(**values)
            )
            if result.rowcount != 1:
                raise KeyError(run_id)

    def record(self, run: JobRun) -> None:
        with self._engine.begin() as conn:
            conn.execute(insert(JobRunRow).values(**_row(run)))

    def latest(self, job: str, session_date: date) -> JobRun | None:
        query = (
            select(JobRunRow)
            .where(JobRunRow.job == job, JobRunRow.session_date == session_date)
            .order_by(JobRunRow.started_at.desc())
            .limit(1)
        )
        with Session(self._engine) as session:
            row = session.scalars(query).first()
            return None if row is None else _from_row(row)

    def has_succeeded(self, job: str, session_date: date) -> bool:
        query = select(func.count()).where(
            JobRunRow.job == job,
            JobRunRow.session_date == session_date,
            JobRunRow.status == JobStatus.SUCCEEDED.value,
        )
        with self._engine.connect() as conn:
            return conn.execute(query).scalar_one() > 0


def _row(run: JobRun) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "job": run.job,
        "session_date": run.session_date,
        "status": run.status.value,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "trading_mode": run.trading_mode,
        "risk_hash": run.risk_hash,
        "details": run.details,
        "error": run.error,
    }


def _from_row(row: JobRunRow) -> JobRun:
    return JobRun(
        run_id=row.run_id,
        job=row.job,
        session_date=row.session_date,
        status=JobStatus(row.status),
        started_at=row.started_at,
        trading_mode=row.trading_mode,
        risk_hash=row.risk_hash,
        finished_at=row.finished_at,
        details=dict(row.details),
        error=row.error,
    )
