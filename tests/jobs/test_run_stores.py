"""The same contract for the in-memory and the Postgres `job_runs` store."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import Engine

from auratrade.db.job_runs import SqlJobRunStore
from auratrade.jobs.base import GuardPolicy, JobContext, JobOutput, JobSpec
from auratrade.jobs.runs import InMemoryJobRunStore, JobRun, JobRunStore, JobStatus

from .conftest import Harness, ny

WED = date(2026, 10, 7)
T0 = datetime(2026, 10, 7, 21, 0, tzinfo=UTC)
STALE = T0 - timedelta(minutes=20)


@pytest.fixture(params=["memory", "postgres"])
def store(request: pytest.FixtureRequest) -> JobRunStore:
    if request.param == "memory":
        return InMemoryJobRunStore()
    engine: Engine = request.getfixturevalue("pg_engine")
    return SqlJobRunStore(engine)


def run(
    job: str = "ingest",
    status: JobStatus = JobStatus.RUNNING,
    started: datetime = T0,
    session: date = WED,
) -> JobRun:
    return JobRun(
        run_id=uuid.uuid4().hex,
        job=job,
        session_date=session,
        status=status,
        started_at=started,
        trading_mode="paper",
        risk_hash="a" * 64,
    )


class TestContract:
    def test_start_and_finish(self, store: JobRunStore) -> None:
        r = run()
        assert store.try_start(r, exclusive=False, stale_before=STALE)
        finished = T0 + timedelta(seconds=90)
        store.finish(r.run_id, JobStatus.SUCCEEDED, finished, details={"bars": 2900, "ok": True})
        latest = store.latest("ingest", WED)
        assert latest is not None
        assert latest.status is JobStatus.SUCCEEDED
        assert latest.details == {"bars": 2900, "ok": True}
        assert latest.duration_s == 90
        assert latest.started_at == T0
        assert latest.risk_hash == "a" * 64
        assert store.has_succeeded("ingest", WED)

    def test_finish_keeps_details_unless_given(self, store: JobRunStore) -> None:
        r = run()
        store.try_start(r, exclusive=False, stale_before=STALE)
        store.finish(r.run_id, JobStatus.FAILED, T0, error="ValueError: x")
        latest = store.latest("ingest", WED)
        assert latest is not None
        assert (latest.details, latest.error) == ({}, "ValueError: x")
        assert not store.has_succeeded("ingest", WED)

    def test_finish_unknown_run(self, store: JobRunStore) -> None:
        with pytest.raises(KeyError):
            store.finish("0" * 32, JobStatus.SUCCEEDED, T0)

    def test_record_and_latest(self, store: JobRunStore) -> None:
        assert store.latest("ingest", WED) is None
        store.record(run(status=JobStatus.SKIPPED_MARKET_CLOSED, started=T0))
        store.record(run(status=JobStatus.WAITING_PREDECESSOR, started=T0 + timedelta(minutes=5)))
        store.record(run(status=JobStatus.SUCCEEDED, session=date(2026, 10, 8)))
        store.record(run(job="scan", status=JobStatus.SUCCEEDED))
        latest = store.latest("ingest", WED)
        assert latest is not None
        assert latest.status is JobStatus.WAITING_PREDECESSOR
        assert not store.has_succeeded("ingest", WED)

    @pytest.mark.parametrize(
        ("existing", "started", "allowed"),
        [
            (JobStatus.SUCCEEDED, T0 - timedelta(hours=3), False),
            (JobStatus.RUNNING, T0 - timedelta(minutes=5), False),
            (JobStatus.RUNNING, T0 - timedelta(minutes=25), True),  # a dead run
            (JobStatus.FAILED, T0 - timedelta(minutes=5), True),
            (JobStatus.WAITING_PREDECESSOR, T0 - timedelta(minutes=5), True),
            (JobStatus.BLOCKED_PREDECESSOR, T0 - timedelta(minutes=5), True),
        ],
    )
    def test_exclusive_start(
        self, store: JobRunStore, existing: JobStatus, started: datetime, allowed: bool
    ) -> None:
        store.record(run(status=existing, started=started))
        assert store.try_start(run(), exclusive=True, stale_before=STALE) is allowed
        # Non-exclusive starts never block; other sessions and jobs never interfere.
        assert store.try_start(run(), exclusive=False, stale_before=STALE)
        assert store.try_start(run(session=date(2026, 10, 8)), exclusive=True, stale_before=STALE)
        assert store.try_start(run(job="scan"), exclusive=True, stale_before=STALE)


def test_concurrent_exclusive_starts_admit_one(pg_engine: Engine) -> None:
    store = SqlJobRunStore(pg_engine)
    barrier = threading.Barrier(6)
    results: list[bool] = []
    lock = threading.Lock()

    def claim() -> None:
        barrier.wait()
        ok = store.try_start(run(), exclusive=True, stale_before=STALE)
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=claim) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False] * 5 + [True]


def test_runner_chain_on_postgres(make_harness: Callable[..., Harness], pg_engine: Engine) -> None:
    from dataclasses import replace

    from auratrade.jobs.base import JobRunner

    h = make_harness(sessions={WED}, now=ny(WED, 16, 20))
    runner = JobRunner(replace(h.deps, store=SqlJobRunStore(pg_engine)))

    def ok(ctx: JobContext) -> JobOutput:
        return {"session": ctx.session_date.isoformat()}

    head = JobSpec(name="post_market", policy=GuardPolicy.EVENING, run=ok)
    nxt = JobSpec(name="ingest", policy=GuardPolicy.EVENING, run=ok)
    assert runner.run(nxt, session=WED).status is JobStatus.WAITING_PREDECESSOR
    assert runner.run(head, session=WED).status is JobStatus.SUCCEEDED
    assert runner.run(nxt, session=WED).status is JobStatus.SUCCEEDED
    assert runner.run(nxt, session=WED).status is JobStatus.SKIPPED_ALREADY_RAN
