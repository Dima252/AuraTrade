"""Quiet jobs (stop_guard): no calendar, no heartbeat unless the job acts or fails."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import pytest

from auratrade.jobs.base import GuardPolicy, JobContext, JobOutput, JobSpec
from auratrade.jobs.runs import JobStatus
from tests.jobs.conftest import Harness


def quiet_spec(output: Callable[[], JobOutput]) -> JobSpec:
    def run(_ctx: JobContext) -> JobOutput:
        return output()

    return JobSpec(name="stop_guard", policy=GuardPolicy.MARKET_HOURS, run=run, quiet=True)


def test_a_quiet_job_records_only_when_it_acts_or_fails(
    make_harness: Callable[..., Harness],
) -> None:
    h = make_harness()
    h.runner.run(quiet_spec(lambda: {"ok": True}))
    assert h.store.runs == []
    h.runner.run(quiet_spec(lambda: {"acted": True, "repaired": ["NVDA"]}))
    (run,) = h.store.runs
    assert run.status is JobStatus.SUCCEEDED
    assert run.details["repaired"] == ["NVDA"]

    def boom() -> JobOutput:
        raise RuntimeError("broker down")

    with pytest.raises(RuntimeError):
        h.runner.run(quiet_spec(boom))
    assert h.store.runs[-1].status is JobStatus.FAILED
    assert any("stop_guard" in t and "broker down" in t for t in h.alerter.texts)


def test_a_failed_quiet_run_is_alerted_even_if_it_cant_be_recorded(
    make_harness: Callable[..., Harness],
) -> None:
    h = make_harness()

    def broken_record(_run: object) -> None:
        raise ConnectionError("database down")

    h.store.record = broken_record  # type: ignore[method-assign]

    def boom() -> JobOutput:
        raise RuntimeError("broker down")

    with pytest.raises(RuntimeError):
        h.runner.run(quiet_spec(boom))
    assert any("broker down" in t for t in h.alerter.texts)


def test_quiet_runs_need_no_calendar(make_harness: Callable[..., Harness]) -> None:
    h = make_harness(sessions=set())
    h.calendar.error = RuntimeError("the calendar must not be read")
    outcome = h.runner.run(quiet_spec(lambda: {"ok": True}), session=date(2026, 10, 24))
    assert outcome.status is JobStatus.SUCCEEDED


def test_only_a_market_hours_job_can_be_quiet() -> None:
    with pytest.raises(ValueError, match="quiet"):
        JobSpec(name="x", policy=GuardPolicy.SESSION_TODAY, run=lambda _c: None, quiet=True)
