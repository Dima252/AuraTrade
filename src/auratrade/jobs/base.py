"""The shared job runner (README §2, §3, §4): calendar guard, `job_runs` heartbeat, structlog
context, alerts, duration check, evening chaining, and the CLI and Lambda entrypoints.

Each job module defines `JOB = JobSpec(...)` and ends with::

    handler = lambda_handler(JOB)

    if __name__ == "__main__":
        raise SystemExit(run_cli(JOB))

**Chaining.** An evening job runs only once its predecessor has succeeded for the same
session; on success it invokes its successor. Its schedule time is a fallback: a run that
finds the predecessor not yet done records `WAITING_PREDECESSOR` and exits, since the
predecessor's success will start it. The scheduler fires it again until its deadline
(fallback + `jobs.chain_wait_minutes`); a run at or after the deadline without a successful
predecessor fails closed (`BLOCKED_PREDECESSOR`) and alerts. A chained job never runs
twice for a session: a run that finds it succeeded, or running, records
`SKIPPED_ALREADY_RAN`.

**Quiet jobs** (`stop_guard`, every 5 minutes) open the database only when they act (README
§5.7, §12): the runner skips the calendar and the RUNNING heartbeat, and records the run only
when it fails or its output says `acted`. The job checks the broker's clock instead.
"""

from __future__ import annotations

import argparse
import importlib
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import structlog
from structlog.typing import FilteringBoundLogger

from auratrade.config import RiskConfig, Settings, TradingMode
from auratrade.jobs import schedule
from auratrade.jobs.invoke import JobInvoker
from auratrade.jobs.runs import JobRun, JobRunStore, JobStatus
from auratrade.log import Redactor
from auratrade.notify.alerts import Alerter
from auratrade.storage import ObjectStore

NY = ZoneInfo("America/New_York")
MAX_ERROR_CHARS = 2000
SATURDAY = 5

log = structlog.get_logger(__name__)


class GuardPolicy(StrEnum):
    """When a job may run (README §4, "Guard policy per job type")."""

    SESSION_TODAY = "session_today"  # morning and intraday jobs
    EVENING = "evening"  # today was a session; chained when in the evening chain
    ANY_DAY = "any_day"  # analyst_collect
    SATURDAY = "saturday"  # weekly_review, regardless of sessions
    WATCHDOG = "watchdog"  # always runs; the job checks only heartbeats expected that day
    MARKET_HOURS = "market_hours"  # stop_guard: the job asks the broker's clock itself


class SessionCalendar(Protocol):
    def is_session(self, day: date) -> bool:
        """Whether `day` is a trading session. Raises when the calendar can't tell."""
        ...


@dataclass(frozen=True)
class JobContext:
    job: str
    session_date: date
    run_id: str
    trading_mode: TradingMode
    settings: Settings
    risk: RiskConfig
    storage: ObjectStore
    started_at: datetime
    log: FilteringBoundLogger

    @property
    def dry_run(self) -> bool:
        return self.trading_mode is TradingMode.DRY_RUN


JobOutput = Mapping[str, Any] | None


@dataclass(frozen=True)
class JobSpec:
    name: str
    policy: GuardPolicy
    run: Callable[[JobContext], JobOutput]
    quiet: bool = False

    def __post_init__(self) -> None:
        if schedule.chain_link(self.name) is not None and self.policy is not GuardPolicy.EVENING:
            raise ValueError(f"{self.name} is in the evening chain; its policy must be EVENING")
        if self.quiet and self.policy is not GuardPolicy.MARKET_HOURS:
            raise ValueError("only a MARKET_HOURS job can be quiet")

    @property
    def chain(self) -> schedule.ChainLink | None:
        return schedule.chain_link(self.name)

    @property
    def predecessor(self) -> str | None:
        return schedule.predecessor(self.name)

    @property
    def successor(self) -> str | None:
        return schedule.successor(self.name)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class RunnerDeps:
    settings: Settings
    risk: RiskConfig
    calendar: SessionCalendar
    store: JobRunStore
    alerter: Alerter
    invoker: JobInvoker
    storage: ObjectStore
    redactor: Redactor = field(default_factory=lambda: Redactor(()))
    clock: Callable[[], datetime] = _utc_now


@dataclass
class _RunState:
    bound: FilteringBoundLogger | None = None
    run: JobRun | None = None
    claimed: bool = False
    job_failed: bool = False


@dataclass(frozen=True)
class RunOutcome:
    job: str
    session_date: date
    status: JobStatus
    run_id: str
    details: Mapping[str, Any] = field(default_factory=dict)


class JobRunner:
    def __init__(self, deps: RunnerDeps) -> None:
        self.deps = deps

    def run(
        self, spec: JobSpec, *, session: date | None = None, force_dry_run: bool = False
    ) -> RunOutcome:
        """Run `spec` once. Any exception (the job's, the calendar's or the store's) is
        recorded and alerted, then re-raised."""
        state = _RunState()
        try:
            return self._run(spec, session, force_dry_run, state)
        except Exception as exc:
            if not state.job_failed:  # the job's own failure is recorded and alerted already
                self._runner_failed(spec, state, exc)
            raise

    def _runner_failed(self, spec: JobSpec, state: _RunState, exc: Exception) -> None:
        """A failure outside the job itself: record it if the store still works, alert."""
        error = self._error(exc)
        bound = state.bound or log.bind(job=spec.name)
        run = state.run
        if run is not None:
            try:
                if state.claimed:
                    self.deps.store.finish(
                        run.run_id, JobStatus.FAILED, self.deps.clock(), error=error
                    )
                else:
                    self.deps.store.record(self._finished(run, JobStatus.FAILED, error=error))
            except Exception as store_exc:
                bound.error("job_run_not_recorded", error=self._error(store_exc))
        bound.error("runner_failed", error=error)
        session = run.session_date if run is not None else "?"
        self._alert(bound, f"{spec.name} {session} FAILED in the job runner: {error}")

    def _run(
        self, spec: JobSpec, session: date | None, force_dry_run: bool, state: _RunState
    ) -> RunOutcome:
        d = self.deps
        started = d.clock()
        session_date = session if session is not None else started.astimezone(NY).date()
        mode = TradingMode.DRY_RUN if force_dry_run else d.settings.trading_mode
        run_id = uuid.uuid4().hex
        bound = log.bind(
            job=spec.name,
            session=session_date.isoformat(),
            run_id=run_id,
            trading_mode=mode.value,
            environment=d.settings.environment,
            risk_hash=d.risk.short_hash,
        )
        new_run = JobRun(
            run_id=run_id,
            job=spec.name,
            session_date=session_date,
            status=JobStatus.RUNNING,
            started_at=started,
            trading_mode=mode.value,
            risk_hash=d.risk.config_hash,
        )
        state.bound, state.run = bound, new_run
        bound.info("job_start")
        if spec.quiet:
            return self._run_quiet(spec, new_run, mode, bound, state)
        skip = self._guard(spec, session_date)
        if skip is not None:
            d.store.record(self._finished(new_run, skip))
            bound.info("job_skipped", status=skip.value)
            return RunOutcome(spec.name, session_date, skip, run_id)

        chained = spec.chain is not None
        stale_before = started - timedelta(minutes=d.risk.jobs.stale_running_minutes)
        if not d.store.try_start(new_run, exclusive=chained, stale_before=stale_before):
            d.store.record(self._finished(new_run, JobStatus.SKIPPED_ALREADY_RAN))
            bound.info("job_skipped", status=JobStatus.SKIPPED_ALREADY_RAN.value)
            return RunOutcome(spec.name, session_date, JobStatus.SKIPPED_ALREADY_RAN, run_id)
        state.claimed = True

        if chained and (waiting := self._check_predecessor(spec, session_date, bound)):
            d.store.finish(run_id, waiting, d.clock())
            return RunOutcome(spec.name, session_date, waiting, run_id)

        ctx = JobContext(
            job=spec.name,
            session_date=session_date,
            run_id=run_id,
            trading_mode=mode,
            settings=d.settings,
            risk=d.risk,
            storage=d.storage,
            started_at=started,
            log=bound,
        )
        try:
            output = spec.run(ctx)
        except Exception as exc:
            finished = d.clock()
            error = self._error(exc)
            d.store.finish(run_id, JobStatus.FAILED, finished, error=error)
            bound.error("job_failed", error=error, duration_s=_seconds(started, finished))
            self._alert(bound, f"{spec.name} {session_date} FAILED: {error}")
            self._check_duration(spec, session_date, started, finished, bound)
            state.job_failed = True
            raise

        finished = d.clock()
        details = dict(output) if output is not None else {}
        d.store.finish(run_id, JobStatus.SUCCEEDED, finished, details=details)
        bound.info("job_succeeded", duration_s=_seconds(started, finished))
        self._check_duration(spec, session_date, started, finished, bound)
        if spec.successor is not None:
            self._invoke_successor(spec.successor, session_date, mode, bound)
        return RunOutcome(spec.name, session_date, JobStatus.SUCCEEDED, run_id, details)

    def _guard(self, spec: JobSpec, session_date: date) -> JobStatus | None:
        match spec.policy:
            case GuardPolicy.SESSION_TODAY | GuardPolicy.EVENING:
                if not self.deps.calendar.is_session(session_date):
                    return JobStatus.SKIPPED_MARKET_CLOSED
            case GuardPolicy.SATURDAY:
                if session_date.weekday() != SATURDAY:
                    return JobStatus.SKIPPED_NOT_SATURDAY
            case GuardPolicy.ANY_DAY | GuardPolicy.WATCHDOG | GuardPolicy.MARKET_HOURS:
                pass
        return None

    def _context(self, run: JobRun, mode: TradingMode, bound: FilteringBoundLogger) -> JobContext:
        d = self.deps
        return JobContext(
            job=run.job,
            session_date=run.session_date,
            run_id=run.run_id,
            trading_mode=mode,
            settings=d.settings,
            risk=d.risk,
            storage=d.storage,
            started_at=run.started_at,
            log=bound,
        )

    def _run_quiet(
        self,
        spec: JobSpec,
        run: JobRun,
        mode: TradingMode,
        bound: FilteringBoundLogger,
        state: _RunState,
    ) -> RunOutcome:
        d = self.deps
        try:
            output = spec.run(self._context(run, mode, bound))
        except Exception as exc:
            error = self._error(exc)
            bound.error("job_failed", error=error)
            self._alert(bound, f"{spec.name} {run.session_date} FAILED: {error}")
            state.job_failed = True
            try:
                d.store.record(self._finished(run, JobStatus.FAILED, error=error))
            except Exception as store_exc:
                bound.error("job_run_not_recorded", error=self._error(store_exc))
            raise
        details = dict(output) if output is not None else {}
        bound.info("job_succeeded", duration_s=_seconds(run.started_at, d.clock()), **details)
        if details.get("acted"):
            d.store.record(replace(self._finished(run, JobStatus.SUCCEEDED), details=details))
        return RunOutcome(spec.name, run.session_date, JobStatus.SUCCEEDED, run.run_id, details)

    def _check_predecessor(
        self, spec: JobSpec, session_date: date, bound: FilteringBoundLogger
    ) -> JobStatus | None:
        """None when the job may run; otherwise the status to record."""
        d = self.deps
        before = spec.predecessor
        link = spec.chain
        if before is None or link is None or d.store.has_succeeded(before, session_date):
            return None
        deadline = datetime.combine(session_date, link.fallback_start, tzinfo=NY) + timedelta(
            minutes=d.risk.jobs.chain_wait_minutes
        )
        if d.clock() < deadline:
            bound.info("job_waiting", predecessor=before, deadline=deadline.isoformat())
            return JobStatus.WAITING_PREDECESSOR
        prior = d.store.latest(before, session_date)
        prior_status = prior.status.value if prior is not None else "no run"
        bound.error("job_blocked", predecessor=before, predecessor_status=prior_status)
        self._alert(
            bound,
            f"{spec.name} {session_date} BLOCKED: {before} has not succeeded "
            f"({prior_status}) by {deadline:%H:%M} ET. No increase of exposure next session.",
        )
        return JobStatus.BLOCKED_PREDECESSOR

    def _check_duration(
        self,
        spec: JobSpec,
        session_date: date,
        started: datetime,
        finished: datetime,
        bound: FilteringBoundLogger,
    ) -> None:
        limit = self.deps.risk.jobs.duration_alert_minutes
        minutes = _seconds(started, finished) / 60
        if minutes > limit:
            bound.warning("job_slow", duration_min=round(minutes, 1), alert_min=limit)
            self._alert(
                bound,
                f"{spec.name} {session_date} took {minutes:.1f} min "
                f"(alert at {limit}; Lambda's limit is 15)",
            )

    def _invoke_successor(
        self, job: str, session_date: date, mode: TradingMode, bound: FilteringBoundLogger
    ) -> None:
        try:
            self.deps.invoker.invoke(job, session_date, dry_run=mode is TradingMode.DRY_RUN)
        except Exception as exc:
            # This job succeeded; the successor's fallback schedule still starts it.
            self._alert(bound, f"could not invoke {job} for {session_date}: {self._error(exc)}")
        else:
            bound.info("successor_invoked", successor=job)

    def _alert(self, bound: FilteringBoundLogger, text: str) -> None:
        settings = self.deps.settings
        message = self.deps.redactor(f"[AuraTrade {settings.environment}] {text}")
        try:
            self.deps.alerter.alert(message)
        except Exception as exc:
            # A failed alert must never hide the error that caused it.
            bound.error("alert_failed", alert=message, error=self._error(exc))

    def _error(self, exc: BaseException) -> str:
        text = self.deps.redactor(f"{type(exc).__name__}: {exc}")
        return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 1] + "…"

    def _finished(self, run: JobRun, status: JobStatus, *, error: str | None = None) -> JobRun:
        return JobRun(
            run_id=run.run_id,
            job=run.job,
            session_date=run.session_date,
            status=status,
            started_at=run.started_at,
            trading_mode=run.trading_mode,
            risk_hash=run.risk_hash,
            finished_at=self.deps.clock(),
            error=error,
        )


def _seconds(start: datetime, end: datetime) -> float:
    return round((end - start).total_seconds(), 3)


# --- entrypoints -------------------------------------------------------------------------

DepsFactory = Callable[[Settings, JobInvoker | None], RunnerDeps]


def load_job_spec(job: str) -> JobSpec:
    """The `JOB` of `auratrade.jobs.<job>`."""
    spec = importlib.import_module(f"auratrade.jobs.{job}").JOB
    if not isinstance(spec, JobSpec):
        raise TypeError(f"auratrade.jobs.{job}.JOB is not a JobSpec")
    return spec


def _parse_session(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value!r}") from exc


def build_parser(spec: JobSpec) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"python -m auratrade.jobs.{spec.name}", description=f"Run the {spec.name} job."
    )
    parser.add_argument(
        "--session", type=_parse_session, help="session date (YYYY-MM-DD); default: today (ET)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="force TRADING_MODE=dry_run for this run"
    )
    parser.add_argument(
        "--chain", action="store_true", help="on success, run the successor job in-process"
    )
    return parser


def run_cli(
    spec: JobSpec, argv: Sequence[str] | None = None, *, deps_factory: DepsFactory | None = None
) -> int:
    """CLI entrypoint. Exit code 0 unless the job (or a job it chained to) raised."""
    from auratrade.jobs import wiring
    from auratrade.jobs.invoke import LocalInvoker, NoChainInvoker

    args = build_parser(spec).parse_args(argv)
    factory = deps_factory or wiring.build_deps
    runners: list[JobRunner] = []
    chain_failures: list[str] = []

    def run_successor(job: str, session_date: date, dry_run: bool) -> None:
        try:
            runners[0].run(load_job_spec(job), session=session_date, force_dry_run=dry_run)
        except Exception:
            # The successor recorded and alerted its own failure; don't alert it twice.
            chain_failures.append(job)

    invoker: JobInvoker = LocalInvoker(run_successor) if args.chain else NoChainInvoker()
    runners.append(JobRunner(factory(wiring.load_runtime_settings(), invoker)))
    try:
        runners[0].run(spec, session=args.session, force_dry_run=args.dry_run)
    except Exception:
        return 1
    return 1 if chain_failures else 0


LambdaHandler = Callable[[Mapping[str, Any] | None, object], dict[str, Any]]


def lambda_handler(spec: JobSpec, *, deps_factory: DepsFactory | None = None) -> LambdaHandler:
    """A Lambda handler for `spec`. The event may carry `session` and `dry_run` (chained
    invocations do); a scheduler event without them runs for today with the configured mode.
    An exception propagates, so Lambda's async retries apply."""

    def handler(event: Mapping[str, Any] | None, _context: object) -> dict[str, Any]:
        from auratrade.jobs import wiring

        payload = event or {}
        session_value = payload.get("session")
        session = date.fromisoformat(session_value) if isinstance(session_value, str) else None
        dry_run = payload.get("dry_run") is True
        factory = deps_factory or wiring.build_deps
        settings = wiring.load_runtime_settings()
        outcome = JobRunner(factory(settings, None)).run(
            spec, session=session, force_dry_run=dry_run
        )
        return {
            "job": outcome.job,
            "session": outcome.session_date.isoformat(),
            "status": outcome.status.value,
            "run_id": outcome.run_id,
        }

    return handler
