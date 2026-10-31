from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from auratrade.config import TradingMode
from auratrade.jobs.base import GuardPolicy, JobContext, JobOutput, JobSpec
from auratrade.jobs.runs import JobRun, JobStatus

from .conftest import SECRET, Harness, ny

WED = date(2026, 10, 7)  # a regular session
SAT = date(2026, 10, 10)
SUN = date(2026, 10, 11)
LABOR_DAY = date(2026, 9, 7)  # Monday holiday
GOOD_FRIDAY = date(2026, 4, 3)
DAY_AFTER_THANKSGIVING = date(2026, 11, 27)  # an early-close session
AFTER_DST_END = date(2026, 11, 2)  # first Monday on EST

SESSIONS = {WED, DAY_AFTER_THANKSGIVING, AFTER_DST_END, date(2026, 10, 9)}

MakeHarness = Callable[..., Harness]


class Spy:
    def __init__(self, output: JobOutput = None, error: Exception | None = None) -> None:
        self.contexts: list[JobContext] = []
        self.output = output
        self.error = error
        self.during: Callable[[], None] | None = None

    def __call__(self, ctx: JobContext) -> JobOutput:
        self.contexts.append(ctx)
        if self.during is not None:
            self.during()
        if self.error is not None:
            raise self.error
        return self.output


def spec(name: str, policy: GuardPolicy, spy: Spy) -> JobSpec:
    return JobSpec(name=name, policy=policy, run=spy)


def succeeded(h: Harness, job: str, day: date) -> None:
    h.store.record(
        JobRun(
            run_id=f"{job}-{day}",
            job=job,
            session_date=day,
            status=JobStatus.SUCCEEDED,
            started_at=h.clock.now - timedelta(minutes=5),
            finished_at=h.clock.now - timedelta(minutes=1),
            trading_mode="paper",
            risk_hash="x",
        )
    )


class TestGuard:
    def test_session_job_runs_and_records_heartbeat(self, make_harness: MakeHarness) -> None:
        h = make_harness(sessions=SESSIONS)
        spy = Spy(output={"bars": 3000})
        outcome = h.runner.run(spec("execute_open", GuardPolicy.SESSION_TODAY, spy), session=WED)
        assert outcome.status is JobStatus.SUCCEEDED
        assert outcome.details == {"bars": 3000}
        (ctx,) = spy.contexts
        assert ctx.session_date == WED
        assert ctx.trading_mode is TradingMode.PAPER
        assert not ctx.dry_run
        (run,) = h.store.runs
        assert run.status is JobStatus.SUCCEEDED
        assert run.details == {"bars": 3000}
        assert run.risk_hash == h.deps.risk.config_hash
        assert run.trading_mode == "paper"
        assert run.duration_s == 0
        assert h.alerter.texts == []

    def test_default_session_is_today_in_new_york(self, make_harness: MakeHarness) -> None:
        # 02:00 UTC on Oct 8 is 22:00 ET on Oct 7.
        h = make_harness(sessions=SESSIONS, now=datetime(2026, 10, 8, 2, 0, tzinfo=UTC))
        spy = Spy()
        h.runner.run(spec("eod_review", GuardPolicy.SESSION_TODAY, spy))
        assert spy.contexts[0].session_date == WED

    def test_dry_run_flag_overrides_paper(self, make_harness: MakeHarness) -> None:
        h = make_harness(sessions=SESSIONS)
        spy = Spy()
        h.runner.run(
            spec("execute_open", GuardPolicy.SESSION_TODAY, spy), session=WED, force_dry_run=True
        )
        assert spy.contexts[0].dry_run
        assert h.store.runs[0].trading_mode == "dry_run"

    @pytest.mark.parametrize("day", [SAT, SUN, LABOR_DAY, GOOD_FRIDAY])
    @pytest.mark.parametrize("policy", [GuardPolicy.SESSION_TODAY, GuardPolicy.EVENING])
    def test_closed_market_skips_with_heartbeat(
        self, make_harness: MakeHarness, day: date, policy: GuardPolicy
    ) -> None:
        h = make_harness(sessions=SESSIONS)
        spy = Spy()
        name = "post_market" if policy is GuardPolicy.EVENING else "premarket_check"
        outcome = h.runner.run(spec(name, policy, spy), session=day)
        assert outcome.status is JobStatus.SKIPPED_MARKET_CLOSED
        assert spy.contexts == []
        (run,) = h.store.runs
        assert run.status is JobStatus.SKIPPED_MARKET_CLOSED
        assert run.finished_at is not None
        assert h.invoker.calls == []

    def test_early_close_session_runs(self, make_harness: MakeHarness) -> None:
        h = make_harness(sessions=SESSIONS)
        spy = Spy()
        h.runner.run(
            spec("premarket_check", GuardPolicy.SESSION_TODAY, spy), session=DAY_AFTER_THANKSGIVING
        )
        assert len(spy.contexts) == 1

    @pytest.mark.parametrize("policy", [GuardPolicy.ANY_DAY, GuardPolicy.WATCHDOG])
    def test_any_day_policies_ignore_the_calendar(
        self, make_harness: MakeHarness, policy: GuardPolicy
    ) -> None:
        h = make_harness(sessions=SESSIONS)
        h.calendar.error = RuntimeError("calendar must not be consulted")
        spy = Spy()
        outcome = h.runner.run(spec("evening_job", policy, spy), session=LABOR_DAY)
        assert outcome.status is JobStatus.SUCCEEDED

    def test_calendar_error_fails_closed(self, make_harness: MakeHarness) -> None:
        h = make_harness(sessions=SESSIONS)
        h.calendar.error = RuntimeError("market_sessions empty")
        spy = Spy()
        with pytest.raises(RuntimeError, match="market_sessions empty"):
            h.runner.run(spec("execute_entries", GuardPolicy.SESSION_TODAY, spy), session=WED)
        assert spy.contexts == []
        (run,) = h.store.runs
        assert run.status is JobStatus.FAILED
        assert run.error == "RuntimeError: market_sessions empty"
        (text,) = h.alerter.texts
        assert "execute_entries 2026-10-07 FAILED in the job runner: RuntimeError" in text


class TestFailures:
    def test_exception_is_recorded_alerted_and_reraised(self, make_harness: MakeHarness) -> None:
        h = make_harness(sessions=SESSIONS)
        spy = Spy(error=ValueError(f"bad response from https://x/bot{SECRET}/y"))
        with pytest.raises(ValueError, match="bad response"):
            h.runner.run(spec("execute_open", GuardPolicy.SESSION_TODAY, spy), session=WED)
        (run,) = h.store.runs
        assert run.status is JobStatus.FAILED
        assert run.error is not None
        assert SECRET not in run.error
        assert "***" in run.error
        (text,) = h.alerter.texts
        assert text.startswith("[AuraTrade dev] execute_open 2026-10-07 FAILED: ValueError")
        assert SECRET not in text

    def test_job_exception_chain_is_preserved(self, make_harness: MakeHarness) -> None:
        h = make_harness(sessions=SESSIONS)
        cause = OSError("socket")
        error = RuntimeError("fetch failed")
        error.__cause__ = cause
        with pytest.raises(RuntimeError) as info:
            h.runner.run(
                spec("execute_open", GuardPolicy.SESSION_TODAY, Spy(error=error)), session=WED
            )
        assert info.value is error
        assert info.value.__cause__ is cause
        assert len(h.alerter.texts) == 1

    def test_store_down_at_start_alerts(
        self, make_harness: MakeHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        h = make_harness(sessions=SESSIONS)

        def down(*args: object, **kwargs: object) -> None:
            raise ConnectionError("database unreachable")

        monkeypatch.setattr(h.store, "try_start", down)
        monkeypatch.setattr(h.store, "record", down)
        spy = Spy()
        with pytest.raises(ConnectionError):
            h.runner.run(spec("execute_open", GuardPolicy.SESSION_TODAY, spy), session=WED)
        assert spy.contexts == []
        (text,) = h.alerter.texts
        assert "FAILED in the job runner: ConnectionError: database unreachable" in text

    def test_store_failure_after_success_alerts(
        self, make_harness: MakeHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        h = make_harness(sessions=SESSIONS)
        original = h.store.finish

        def finish(run_id: str, status: JobStatus, *args: object, **kwargs: object) -> None:
            if status is JobStatus.SUCCEEDED:
                raise ConnectionError("connection reset")
            original(run_id, status, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(h.store, "finish", finish)
        with pytest.raises(ConnectionError):
            h.runner.run(spec("execute_open", GuardPolicy.SESSION_TODAY, Spy()), session=WED)
        (run,) = h.store.runs
        assert run.status is JobStatus.FAILED
        assert run.error == "ConnectionError: connection reset"
        assert len(h.alerter.texts) == 1

    def test_long_error_is_truncated(self, make_harness: MakeHarness) -> None:
        h = make_harness(sessions=SESSIONS)
        with pytest.raises(ValueError, match="x"):
            h.runner.run(
                spec("scan_x", GuardPolicy.SESSION_TODAY, Spy(error=ValueError("x" * 5000))),
                session=WED,
            )
        error = h.store.runs[0].error
        assert error is not None
        assert len(error) == 2000

    def test_failing_alert_does_not_mask_the_error(self, make_harness: MakeHarness) -> None:
        h = make_harness(sessions=SESSIONS)
        h.alerter.error = ConnectionError("telegram down")
        with pytest.raises(KeyError):
            h.runner.run(
                spec("execute_open", GuardPolicy.SESSION_TODAY, Spy(error=KeyError("k"))),
                session=WED,
            )
        assert h.store.runs[0].status is JobStatus.FAILED

    @pytest.mark.parametrize(("minutes", "alerts"), [(10, 0), (10.5, 1)])
    def test_duration_alert(self, make_harness: MakeHarness, minutes: float, alerts: int) -> None:
        h = make_harness(sessions=SESSIONS)
        spy = Spy()
        spy.during = lambda: h.clock.advance(minutes=minutes)
        h.runner.run(spec("scan_x", GuardPolicy.SESSION_TODAY, spy), session=WED)
        assert len(h.alerter.texts) == alerts
        if alerts:
            assert "took 10.5 min" in h.alerter.texts[0]

    def test_slow_failure_alerts_twice(self, make_harness: MakeHarness) -> None:
        h = make_harness(sessions=SESSIONS)
        spy = Spy(error=TimeoutError("vendor"))
        spy.during = lambda: h.clock.advance(minutes=12)
        with pytest.raises(TimeoutError):
            h.runner.run(spec("scan_x", GuardPolicy.SESSION_TODAY, spy), session=WED)
        assert len(h.alerter.texts) == 2


class TestChaining:
    def test_head_of_chain_runs_and_invokes_successor(self, make_harness: MakeHarness) -> None:
        h = make_harness(sessions=SESSIONS, now=ny(WED, 16, 20))
        outcome = h.runner.run(spec("post_market", GuardPolicy.EVENING, Spy()), session=WED)
        assert outcome.status is JobStatus.SUCCEEDED
        assert h.invoker.calls == [("ingest", WED, False)]

    def test_dry_run_propagates_to_successor(self, make_harness: MakeHarness) -> None:
        h = make_harness(sessions=SESSIONS, now=ny(WED, 16, 20))
        h.runner.run(
            spec("post_market", GuardPolicy.EVENING, Spy()), session=WED, force_dry_run=True
        )
        assert h.invoker.calls == [("ingest", WED, True)]

    def test_runs_once_predecessor_succeeded(self, make_harness: MakeHarness) -> None:
        h = make_harness(sessions=SESSIONS, now=ny(WED, 17, 3))
        succeeded(h, "post_market", WED)
        spy = Spy()
        assert (
            h.runner.run(spec("ingest", GuardPolicy.EVENING, spy), session=WED).status
            is JobStatus.SUCCEEDED
        )
        assert h.invoker.calls == [("simulate", WED, False)]

    def test_end_of_chain_invokes_nothing(self, make_harness: MakeHarness) -> None:
        h = make_harness(sessions=SESSIONS, now=ny(WED, 17, 50))
        succeeded(h, "scan", WED)
        h.runner.run(spec("evening_job", GuardPolicy.EVENING, Spy()), session=WED)
        assert h.invoker.calls == []

    def test_predecessor_for_another_session_does_not_count(
        self, make_harness: MakeHarness
    ) -> None:
        h = make_harness(sessions=SESSIONS, now=ny(WED, 17, 0))
        succeeded(h, "post_market", date(2026, 10, 6))
        spy = Spy()
        status = h.runner.run(spec("ingest", GuardPolicy.EVENING, spy), session=WED).status
        assert status is JobStatus.WAITING_PREDECESSOR
        assert spy.contexts == []

    def test_waits_before_deadline_without_alert(self, make_harness: MakeHarness) -> None:
        h = make_harness(sessions=SESSIONS, now=ny(WED, 17, 29))
        spy = Spy()
        outcome = h.runner.run(spec("ingest", GuardPolicy.EVENING, spy), session=WED)
        assert outcome.status is JobStatus.WAITING_PREDECESSOR
        assert spy.contexts == []
        assert h.alerter.texts == []
        assert h.store.runs[0].status is JobStatus.WAITING_PREDECESSOR
        assert h.invoker.calls == []

    def test_blocks_and_alerts_at_deadline(self, make_harness: MakeHarness) -> None:
        h = make_harness(sessions=SESSIONS, now=ny(WED, 17, 25))
        h.store.record(
            JobRun("pm-1", "post_market", WED, JobStatus.FAILED, ny(WED, 16, 20), "paper", "x")
        )
        h.clock.now = ny(WED, 17, 30)
        spy = Spy()
        outcome = h.runner.run(spec("ingest", GuardPolicy.EVENING, spy), session=WED)
        assert outcome.status is JobStatus.BLOCKED_PREDECESSOR
        assert spy.contexts == []
        (text,) = h.alerter.texts
        assert "ingest 2026-10-07 BLOCKED: post_market has not succeeded (FAILED)" in text
        assert "by 17:30 ET" in text

    def test_block_reports_a_missing_predecessor(self, make_harness: MakeHarness) -> None:
        h = make_harness(sessions=SESSIONS, now=ny(WED, 18, 0))
        h.runner.run(spec("regime", GuardPolicy.EVENING, Spy()), session=WED)
        assert "simulate has not succeeded (no run)" in h.alerter.texts[0]

    def test_deadline_follows_new_york_time_across_dst(self, make_harness: MakeHarness) -> None:
        # Nov 2 is on EST: ingest's deadline 17:30 ET is 22:30 UTC (21:30 UTC in October).
        h = make_harness(sessions=SESSIONS, now=datetime(2026, 11, 2, 22, 29, tzinfo=UTC))
        job = spec("ingest", GuardPolicy.EVENING, Spy())
        assert h.runner.run(job, session=AFTER_DST_END).status is JobStatus.WAITING_PREDECESSOR
        h.clock.now = datetime(2026, 11, 2, 22, 30, tzinfo=UTC)
        assert h.runner.run(job, session=AFTER_DST_END).status is JobStatus.BLOCKED_PREDECESSOR

    def test_late_predecessor_still_starts_successor(self, make_harness: MakeHarness) -> None:
        h = make_harness(sessions=SESSIONS, now=ny(WED, 17, 45))
        job = spec("ingest", GuardPolicy.EVENING, Spy())
        assert h.runner.run(job, session=WED).status is JobStatus.BLOCKED_PREDECESSOR
        succeeded(h, "post_market", WED)  # e.g. re-run by hand
        assert h.runner.run(job, session=WED).status is JobStatus.SUCCEEDED

    def test_chained_job_never_runs_twice(self, make_harness: MakeHarness) -> None:
        h = make_harness(sessions=SESSIONS, now=ny(WED, 17, 3))
        succeeded(h, "post_market", WED)
        spy = Spy()
        job = spec("ingest", GuardPolicy.EVENING, spy)
        h.runner.run(job, session=WED)
        h.clock.advance(minutes=1)
        second = h.runner.run(job, session=WED)
        assert second.status is JobStatus.SKIPPED_ALREADY_RAN
        assert len(spy.contexts) == 1
        assert len(h.invoker.calls) == 1

    def test_running_chained_job_blocks_a_duplicate(self, make_harness: MakeHarness) -> None:
        h = make_harness(sessions=SESSIONS, now=ny(WED, 17, 10))
        succeeded(h, "post_market", WED)
        h.store.record(
            JobRun("live", "ingest", WED, JobStatus.RUNNING, ny(WED, 17, 0), "paper", "x")
        )
        spy = Spy()
        status = h.runner.run(spec("ingest", GuardPolicy.EVENING, spy), session=WED).status
        assert status is JobStatus.SKIPPED_ALREADY_RAN
        assert spy.contexts == []

    def test_stale_running_row_does_not_block(self, make_harness: MakeHarness) -> None:
        h = make_harness(sessions=SESSIONS, now=ny(WED, 17, 25))
        succeeded(h, "post_market", WED)
        # A run that died 25 minutes ago (Lambda's limit is 15) without finishing.
        h.store.record(
            JobRun("dead", "ingest", WED, JobStatus.RUNNING, ny(WED, 17, 0), "paper", "x")
        )
        status = h.runner.run(spec("ingest", GuardPolicy.EVENING, Spy()), session=WED).status
        assert status is JobStatus.SUCCEEDED

    def test_failed_chained_job_can_rerun(self, make_harness: MakeHarness) -> None:
        h = make_harness(sessions=SESSIONS, now=ny(WED, 17, 3))
        succeeded(h, "post_market", WED)
        job = spec("ingest", GuardPolicy.EVENING, Spy(error=RuntimeError("vendor down")))
        with pytest.raises(RuntimeError):
            h.runner.run(job, session=WED)
        assert h.invoker.calls == []
        fixed = replace(job, run=Spy())
        assert h.runner.run(fixed, session=WED).status is JobStatus.SUCCEEDED

    def test_invoke_failure_alerts_but_job_succeeded(self, make_harness: MakeHarness) -> None:
        h = make_harness(sessions=SESSIONS, now=ny(WED, 16, 20))
        h.invoker.error = RuntimeError("throttled")
        outcome = h.runner.run(spec("post_market", GuardPolicy.EVENING, Spy()), session=WED)
        assert outcome.status is JobStatus.SUCCEEDED
        assert (
            "could not invoke ingest for 2026-10-07: RuntimeError: throttled" in h.alerter.texts[0]
        )

    def test_unchained_jobs_may_run_many_times(self, make_harness: MakeHarness) -> None:
        h = make_harness(sessions=SESSIONS)
        spy = Spy()
        job = spec("intraday_monitor", GuardPolicy.SESSION_TODAY, spy)
        for _ in range(3):
            assert h.runner.run(job, session=WED).status is JobStatus.SUCCEEDED
        assert len(spy.contexts) == 3


def test_chain_jobs_must_use_the_evening_policy() -> None:
    with pytest.raises(ValueError, match="evening chain"):
        JobSpec(name="ingest", policy=GuardPolicy.SESSION_TODAY, run=Spy())


def test_spec_chain_properties() -> None:
    ingest = JobSpec(name="ingest", policy=GuardPolicy.EVENING, run=Spy())
    assert (ingest.predecessor, ingest.successor) == ("post_market", "simulate")
    other = JobSpec(name="eod_review", policy=GuardPolicy.SESSION_TODAY, run=Spy())
    assert (other.chain, other.predecessor, other.successor) == (None, None, None)
