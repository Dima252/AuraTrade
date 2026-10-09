"""The job runner's calendar guard on a synced `market_sessions` (tasks 1.1 + 1.4)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine

from auratrade.calendar.sessions import DbCalendar
from auratrade.calendar.sync import sync_market_sessions
from auratrade.jobs.base import GuardPolicy, JobContext, JobOutput, JobRunner, JobSpec
from auratrade.jobs.runs import JobStatus
from tests.calendar_fixture import alpaca_times

from .conftest import Harness, ny

WED = date(2026, 10, 7)
GOOD_FRIDAY = date(2026, 4, 3)
NOW = datetime(2026, 10, 9, 21, 0, tzinfo=UTC)


def sync(engine: Engine) -> None:
    times = alpaca_times()
    sync_market_sessions(
        engine, times, times, start=date(2025, 12, 1), end=date(2027, 1, 31), now=NOW
    )


class TestDbCalendarInTheRunner:
    def test_guard_reads_market_sessions(
        self, pg_engine: Engine, make_harness: Callable[..., Harness]
    ) -> None:
        sync(pg_engine)
        h = make_harness(now=ny(WED, 7, 30))
        runner = JobRunner(replace(h.deps, calendar=DbCalendar(pg_engine)))
        calls: list[date] = []

        def run(ctx: JobContext) -> JobOutput:
            calls.append(ctx.session_date)
            return None

        job = JobSpec(name="premarket_check", policy=GuardPolicy.SESSION_TODAY, run=run)
        assert runner.run(job, session=WED).status is JobStatus.SUCCEEDED
        assert runner.run(job, session=GOOD_FRIDAY).status is JobStatus.SKIPPED_MARKET_CLOSED
        assert calls == [WED]

    def test_unsynced_date_fails_closed_with_alert(
        self, pg_engine: Engine, make_harness: Callable[..., Harness]
    ) -> None:
        sync(pg_engine)
        h = make_harness(now=ny(WED, 7, 30))
        runner = JobRunner(replace(h.deps, calendar=DbCalendar(pg_engine)))
        job = JobSpec(name="premarket_check", policy=GuardPolicy.SESSION_TODAY, run=lambda c: None)
        with pytest.raises(Exception, match="outside the synced calendar"):
            runner.run(job, session=date(2027, 3, 1))
        assert h.store.runs[-1].status is JobStatus.FAILED
        assert "outside the synced calendar" in h.alerter.texts[0]

    def test_empty_table_fails_closed(
        self, pg_engine: Engine, make_harness: Callable[..., Harness]
    ) -> None:
        h = make_harness(now=ny(WED, 7, 30))
        runner = JobRunner(replace(h.deps, calendar=DbCalendar(pg_engine)))
        job = JobSpec(name="premarket_check", policy=GuardPolicy.SESSION_TODAY, run=lambda c: None)
        with pytest.raises(Exception, match="empty"):
            runner.run(job, session=WED)
