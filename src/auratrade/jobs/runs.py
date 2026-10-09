"""`job_runs` heartbeats: the record every job run leaves, and the store interface.

The Postgres store arrives with the `job_runs` table (task 1.2); the in-memory store
serves tests and has the same semantics.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Protocol


class JobStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED_MARKET_CLOSED = "SKIPPED_MARKET_CLOSED"
    SKIPPED_NOT_SATURDAY = "SKIPPED_NOT_SATURDAY"
    # A chained job that already succeeded (or is running) for the session.
    SKIPPED_ALREADY_RAN = "SKIPPED_ALREADY_RAN"
    # A chained job whose predecessor hasn't succeeded yet, before its deadline.
    WAITING_PREDECESSOR = "WAITING_PREDECESSOR"
    # The same, at or after the deadline: fail closed and alert.
    BLOCKED_PREDECESSOR = "BLOCKED_PREDECESSOR"


@dataclass(frozen=True)
class JobRun:
    run_id: str
    job: str
    session_date: date
    status: JobStatus
    started_at: datetime
    trading_mode: str
    risk_hash: str
    finished_at: datetime | None = None
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def duration_s(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


class JobRunStore(Protocol):
    def try_start(self, run: JobRun, *, exclusive: bool, stale_before: datetime) -> bool:
        """Insert a RUNNING row. With `exclusive`, refuse (return False) when the job
        already succeeded for the session or has a RUNNING row started after
        `stale_before`. The check and the insert are atomic."""
        ...

    def finish(
        self,
        run_id: str,
        status: JobStatus,
        finished_at: datetime,
        *,
        details: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None: ...

    def record(self, run: JobRun) -> None:
        """Insert a finished row (skips write a heartbeat too)."""
        ...

    def latest(self, job: str, session_date: date) -> JobRun | None: ...

    def has_succeeded(self, job: str, session_date: date) -> bool: ...


class InMemoryJobRunStore:
    def __init__(self) -> None:
        self._runs: dict[str, JobRun] = {}
        self._lock = threading.Lock()

    @property
    def runs(self) -> list[JobRun]:
        return list(self._runs.values())

    def try_start(self, run: JobRun, *, exclusive: bool, stale_before: datetime) -> bool:
        with self._lock:
            if exclusive and any(
                self._blocks(existing, run, stale_before) for existing in self._runs.values()
            ):
                return False
            self._runs[run.run_id] = run
            return True

    @staticmethod
    def _blocks(existing: JobRun, new: JobRun, stale_before: datetime) -> bool:
        if (existing.job, existing.session_date) != (new.job, new.session_date):
            return False
        if existing.status is JobStatus.SUCCEEDED:
            return True
        return existing.status is JobStatus.RUNNING and existing.started_at > stale_before

    def finish(
        self,
        run_id: str,
        status: JobStatus,
        finished_at: datetime,
        *,
        details: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            run = self._runs[run_id]
            self._runs[run_id] = replace(
                run,
                status=status,
                finished_at=finished_at,
                details=details if details is not None else run.details,
                error=error,
            )

    def record(self, run: JobRun) -> None:
        with self._lock:
            self._runs[run.run_id] = run

    def latest(self, job: str, session_date: date) -> JobRun | None:
        with self._lock:
            matching = [
                r for r in self._runs.values() if (r.job, r.session_date) == (job, session_date)
            ]
        return max(matching, key=lambda r: r.started_at, default=None)

    def has_succeeded(self, job: str, session_date: date) -> bool:
        with self._lock:
            return any(
                (r.job, r.session_date, r.status) == (job, session_date, JobStatus.SUCCEEDED)
                for r in self._runs.values()
            )
