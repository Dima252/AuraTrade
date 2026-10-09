from __future__ import annotations

import json
import sys
import types
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber

from auratrade.calendar.sessions import DbCalendar
from auratrade.config import Settings
from auratrade.db.job_runs import SqlJobRunStore
from auratrade.jobs import base, schedule, wiring
from auratrade.jobs.base import GuardPolicy, JobContext, JobOutput, JobSpec, RunnerDeps
from auratrade.jobs.invoke import JobInvoker, LambdaInvoker, LocalInvoker, NoChainInvoker
from auratrade.jobs.runs import JobStatus
from auratrade.notify.alerts import LogAlerter
from auratrade.notify.telegram import TelegramNotifier

from .conftest import Harness, ny

WED = date(2026, 10, 7)
MakeHarness = Callable[..., Harness]


def ok_job(name: str = "post_market", policy: GuardPolicy = GuardPolicy.EVENING) -> JobSpec:
    def run(ctx: JobContext) -> JobOutput:
        return {"dry_run": ctx.dry_run}

    return JobSpec(name=name, policy=policy, run=run)


def failing_job(name: str, policy: GuardPolicy = GuardPolicy.EVENING) -> JobSpec:
    def run(ctx: JobContext) -> JobOutput:
        raise RuntimeError("boom")

    return JobSpec(name=name, policy=policy, run=run)


@pytest.fixture
def harness(make_harness: MakeHarness, monkeypatch: pytest.MonkeyPatch) -> Harness:
    h = make_harness(sessions={WED}, now=ny(WED, 16, 20))
    # Never read the developer's real .env in tests.
    monkeypatch.setattr(wiring, "load_runtime_settings", lambda: h.deps.settings)
    return h


def factory_for(h: Harness, seen: list[JobInvoker | None]) -> base.DepsFactory:
    def factory(settings: Settings, invoker: JobInvoker | None) -> RunnerDeps:
        seen.append(invoker)
        return replace(h.deps, invoker=invoker) if invoker is not None else h.deps

    return factory


class TestCli:
    def test_runs_with_session_and_dry_run(self, harness: Harness) -> None:
        seen: list[JobInvoker | None] = []
        code = base.run_cli(
            ok_job(),
            ["--session", "2026-10-07", "--dry-run"],
            deps_factory=factory_for(harness, seen),
        )
        assert code == 0
        (run,) = harness.store.runs
        assert run.status is JobStatus.SUCCEEDED
        assert run.details == {"dry_run": True}
        assert isinstance(seen[0], NoChainInvoker)

    def test_failure_exits_1(self, harness: Harness) -> None:
        code = base.run_cli(
            failing_job("post_market"),
            ["--session", "2026-10-07"],
            deps_factory=factory_for(harness, []),
        )
        assert code == 1
        assert harness.store.runs[0].status is JobStatus.FAILED

    def test_bad_session_argument(
        self, harness: Harness, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            base.run_cli(
                ok_job(), ["--session", "07/10/2026"], deps_factory=factory_for(harness, [])
            )
        assert "YYYY-MM-DD" in capsys.readouterr().err

    def test_chain_runs_successors_in_process(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        jobs = {link.job: ok_job(link.job) for link in schedule.EVENING_CHAIN}
        monkeypatch.setattr(base, "load_job_spec", jobs.__getitem__)
        seen: list[JobInvoker | None] = []
        code = base.run_cli(
            jobs["post_market"],
            ["--session", "2026-10-07", "--chain"],
            deps_factory=factory_for(harness, seen),
        )
        assert code == 0
        assert isinstance(seen[0], LocalInvoker)
        assert [r.job for r in harness.store.runs] == [link.job for link in schedule.EVENING_CHAIN]
        assert all(r.status is JobStatus.SUCCEEDED for r in harness.store.runs)

    def test_chain_failure_exits_1_without_a_second_alert(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        jobs = {"ingest": failing_job("ingest")}
        monkeypatch.setattr(base, "load_job_spec", jobs.__getitem__)
        code = base.run_cli(
            ok_job(), ["--session", "2026-10-07", "--chain"], deps_factory=factory_for(harness, [])
        )
        assert code == 1
        assert [(r.job, r.status) for r in harness.store.runs] == [
            ("post_market", JobStatus.SUCCEEDED),
            ("ingest", JobStatus.FAILED),
        ]
        (text,) = harness.alerter.texts
        assert "ingest 2026-10-07 FAILED" in text


class TestLambdaHandler:
    def test_chained_event(self, harness: Harness) -> None:
        handler = base.lambda_handler(ok_job(), deps_factory=factory_for(harness, []))
        result = handler({"session": "2026-10-07", "dry_run": True}, None)
        assert result["status"] == "SUCCEEDED"
        assert result["session"] == "2026-10-07"
        assert harness.store.runs[0].details == {"dry_run": True}
        assert harness.invoker.calls == [("ingest", WED, True)]

    @pytest.mark.parametrize("event", [None, {}, {"dry_run": "yes"}])
    def test_scheduler_event_runs_today_in_configured_mode(
        self, harness: Harness, event: dict[str, object] | None
    ) -> None:
        handler = base.lambda_handler(ok_job(), deps_factory=factory_for(harness, []))
        result = handler(event, None)
        assert result["session"] == "2026-10-07"
        assert harness.store.runs[0].trading_mode == "paper"

    def test_exception_propagates_for_lambda_retries(self, harness: Harness) -> None:
        handler = base.lambda_handler(
            failing_job("post_market"), deps_factory=factory_for(harness, [])
        )
        with pytest.raises(RuntimeError, match="boom"):
            handler({"session": "2026-10-07"}, None)


class TestLoadJobSpec:
    def test_loads_job_attribute(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = types.ModuleType("auratrade.jobs.fake_job")
        module.JOB = ok_job("fake_job", GuardPolicy.ANY_DAY)  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "auratrade.jobs.fake_job", module)
        assert base.load_job_spec("fake_job").name == "fake_job"

    def test_rejects_non_spec(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = types.ModuleType("auratrade.jobs.bad_job")
        module.JOB = "not a spec"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "auratrade.jobs.bad_job", module)
        with pytest.raises(TypeError, match="not a JobSpec"):
            base.load_job_spec("bad_job")

    def test_missing_module(self) -> None:
        with pytest.raises(ModuleNotFoundError):
            base.load_job_spec("no_such_job")


def test_lambda_invoker_sends_async_event() -> None:
    client = boto3.client(
        "lambda",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    invoker = LambdaInvoker(client, function_template="auratrade-{env}-{job}", environment="paper")
    with Stubber(client) as stub:
        stub.add_response(
            "invoke",
            {"StatusCode": 202},
            {
                "FunctionName": "auratrade-paper-simulate",
                "InvocationType": "Event",
                "Payload": json.dumps({"session": "2026-10-07", "dry_run": False}).encode(),
            },
        )
        invoker.invoke("simulate", WED, dry_run=False)
        stub.assert_no_pending_responses()


def test_no_chain_invoker_only_logs() -> None:
    NoChainInvoker().invoke("ingest", WED, dry_run=True)


class TestSchedule:
    def test_chain_order_matches_readme(self) -> None:
        assert [link.job for link in schedule.EVENING_CHAIN] == [
            "post_market",
            "ingest",
            "simulate",
            "regime",
            "scan",
            "analyst_submit",
        ]
        starts = [link.fallback_start for link in schedule.EVENING_CHAIN]
        assert starts == sorted(starts)

    def test_neighbours(self) -> None:
        assert schedule.predecessor("post_market") is None
        assert schedule.successor("analyst_submit") is None
        assert schedule.predecessor("scan") == "regime"
        assert schedule.successor("scan") == "analyst_submit"
        assert schedule.predecessor("analyst_collect") is None
        assert schedule.successor("analyst_collect") is None
        assert schedule.chain_link("watchdog") is None


class TestWiring:
    def test_alerter_choice(self) -> None:
        bare = Settings(_env_file=None)  # type: ignore[call-arg]
        assert isinstance(wiring.make_alerter(bare), LogAlerter)
        token_only = Settings(_env_file=None, telegram_bot_token="t")  # type: ignore[call-arg]
        assert isinstance(wiring.make_alerter(token_only), LogAlerter)
        both = Settings(_env_file=None, telegram_bot_token="t", telegram_chat_id="1")  # type: ignore[call-arg]
        assert isinstance(wiring.make_alerter(both), TelegramNotifier)

    def test_build_deps_wires_postgres_and_the_calendar(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            database_url="postgresql+psycopg://u:p@localhost:1/x",
            storage_local_dir=tmp_path,
        )
        deps = wiring.build_deps(settings, NoChainInvoker())
        assert isinstance(deps.store, SqlJobRunStore)
        assert isinstance(deps.calendar, DbCalendar)
        assert isinstance(deps.invoker, NoChainInvoker)
        # Nothing connects until the runner asks (port 1 is closed).
        lambda_deps = wiring.build_deps(settings, None)
        assert isinstance(lambda_deps.invoker, LambdaInvoker)

    def test_needs_a_database_url(self) -> None:
        with pytest.raises(ValueError, match="DATABASE_URL"):
            wiring.build_deps(Settings(_env_file=None), NoChainInvoker())  # type: ignore[call-arg]
