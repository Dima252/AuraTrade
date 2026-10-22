"""Synthesizes the trading stack offline (no AWS calls, no image build) and checks what it
promises: the functions and their handlers, the `dry_run` default, retries only for order jobs,
schedules in New York time that match `jobs.schedule`, and access granted only where needed."""

from __future__ import annotations

import json
from datetime import time
from typing import Any

import pytest

cdk = pytest.importorskip("aws_cdk")
assertions = pytest.importorskip("aws_cdk.assertions")

from stacks.trading import (  # noqa: E402
    FUNCTIONS,
    TIMEZONE,
    TradingStack,
    cron_expressions,
    function_name,
)

from auratrade.config import load_risk_config  # noqa: E402
from auratrade.jobs import schedule  # noqa: E402

Match = assertions.Match
Template = assertions.Template

ENV = cdk.Environment(account="123456789012", region="us-east-1")
ORDER_JOBS = {"execute_open", "execute_entries", "eod_review", "post_market"}


def synth(**kwargs: Any) -> Any:
    app = cdk.App()
    stack = TradingStack(app, "Trading", env_name="paper", env=ENV, **kwargs)
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template() -> Any:
    return synth()


@pytest.fixture(scope="module")
def paper_template() -> Any:
    return synth(trading_mode="paper")


def functions(template: Any) -> dict[str, dict[str, Any]]:
    """The Lambda functions by job name."""
    out = {}
    for resource in template.find_resources("AWS::Lambda::Function").values():
        props = resource["Properties"]
        out[props["FunctionName"].removeprefix("auratrade-paper-")] = props
    return out


def logical_id(template: Any, job: str) -> str:
    for key, resource in template.find_resources("AWS::Lambda::Function").items():
        if resource["Properties"]["FunctionName"] == function_name("paper", job):
            return str(key)
    raise KeyError(job)


def policy_text(template: Any, job: str) -> str:
    """Every IAM policy attached to the job's role, as JSON text."""
    role = functions(template)[job]["Role"]["Fn::GetAtt"][0]
    return json.dumps(
        [
            p["Properties"]["PolicyDocument"]
            for p in template.find_resources("AWS::IAM::Policy").values()
            if {"Ref": role} in p["Properties"]["Roles"]
        ]
    )


def test_one_function_per_job_on_one_image(template: Any) -> None:
    fns = functions(template)
    assert set(fns) == {f.job for f in FUNCTIONS}
    assert {s.job for s in schedule.skeleton_slots(30)} <= set(fns)
    images = {json.dumps(p["Code"]["ImageUri"]) for p in fns.values()}
    assert len(images) == 1
    for job, props in fns.items():
        assert props["PackageType"] == "Image"
        assert props["ImageConfig"]["Command"] == [f"auratrade.jobs.{job}.handler"]
        assert props["Architectures"] == ["x86_64"]
        assert props["Timeout"] <= 900


def test_the_trading_mode_defaults_to_dry_run(template: Any, paper_template: Any) -> None:
    for props in functions(template).values():
        env = props["Environment"]["Variables"]
        assert env["TRADING_MODE"] == "dry_run"
        assert env["ENVIRONMENT"] == "paper"
        assert env["SSM_PARAMETER_PATH"] == "/auratrade/paper/"
        assert env["STORAGE_BACKEND"] == "s3"
        assert env["SCHEDULE_GROUP"] == "auratrade-paper"
    for props in functions(paper_template).values():
        assert props["Environment"]["Variables"]["TRADING_MODE"] == "paper"


def test_no_secret_reaches_the_template(paper_template: Any) -> None:
    text = json.dumps(paper_template.to_json())
    for key in ("ALPACA_API_KEY", "ANTHROPIC_API_KEY", "DATABASE_URL", "TELEGRAM_BOT_TOKEN"):
        assert key not in text
    assert "HEALTHCHECKS" not in text


@pytest.mark.parametrize("mode", ["live", "LIVE", ""])
def test_live_and_unknown_modes_are_refused(mode: str) -> None:
    with pytest.raises(ValueError, match="refused"):
        TradingStack(cdk.App(), "Trading", env_name="paper", trading_mode=mode, env=ENV)


def test_unknown_environments_are_refused() -> None:
    with pytest.raises(ValueError, match="unknown environment"):
        TradingStack(cdk.App(), "Trading", env_name="prod", env=ENV)


def test_async_retries_only_for_order_jobs(template: Any) -> None:
    configs = {
        c["Properties"]["FunctionName"]["Ref"]: c["Properties"]
        for c in template.find_resources("AWS::Lambda::EventInvokeConfig").values()
    }
    for spec in FUNCTIONS:
        config = configs[logical_id(template, spec.job)]
        expected = 2 if spec.job in ORDER_JOBS else 0
        assert config["MaximumRetryAttempts"] == expected, spec.job
        assert config["MaximumEventAgeInSeconds"] == 3600
    assert {f.job for f in FUNCTIONS if f.retries} == ORDER_JOBS


def test_every_function_reads_only_its_parameter_path(template: Any) -> None:
    for spec in FUNCTIONS:
        text = policy_text(template, spec.job)
        assert "ssm:GetParametersByPath" in text
        # The path's own ARN, trailing slash included: AWS authorizes the call against it
        # (without the slash every call was denied on the first deploy).
        assert ':parameter/auratrade/paper/"' in text
        assert 'ssm:GetParameters"' not in text


def test_only_storage_jobs_reach_the_bucket(template: Any) -> None:
    for spec in FUNCTIONS:
        assert ("s3:PutObject" in policy_text(template, spec.job)) is spec.storage, spec.job
    assert {f.job for f in FUNCTIONS if f.storage} == {"post_market", "ingest"}


def test_the_bucket_is_private_and_kept(template: Any) -> None:
    template.has_resource(
        "AWS::S3::Bucket",
        {
            "DeletionPolicy": "Retain",
            "Properties": Match.object_like(
                {
                    "PublicAccessBlockConfiguration": {
                        "BlockPublicAcls": True,
                        "BlockPublicPolicy": True,
                        "IgnorePublicAcls": True,
                        "RestrictPublicBuckets": True,
                    }
                }
            ),
        },
    )


def test_each_chain_job_may_invoke_only_its_successor(template: Any) -> None:
    for spec in FUNCTIONS:
        text = policy_text(template, spec.job)
        nxt = schedule.successor(spec.job)
        if nxt is None:
            assert "lambda:InvokeFunction" not in text, spec.job
        else:
            assert "lambda:InvokeFunction" in text
            assert logical_id(template, nxt) in text
            others = {
                logical_id(template, f.job) for f in FUNCTIONS if f.job not in (nxt, spec.job)
            }
            assert not any(o in text for o in others), spec.job


def test_only_the_watchdog_lists_schedules(template: Any) -> None:
    for spec in FUNCTIONS:
        assert ("scheduler:ListSchedules" in policy_text(template, spec.job)) is (
            spec.job == "watchdog"
        )


def test_log_groups_keep_a_month(template: Any) -> None:
    groups = template.find_resources("AWS::Logs::LogGroup")
    assert len(groups) == len(FUNCTIONS)
    for group in groups.values():
        assert group["Properties"]["RetentionInDays"] == 30
        assert group["Properties"]["LogGroupName"].startswith("/aws/lambda/auratrade-paper-")


def schedules(template: Any) -> list[dict[str, Any]]:
    return [r["Properties"] for r in template.find_resources("AWS::Scheduler::Schedule").values()]


def test_schedules_fire_in_new_york_on_weekdays(template: Any) -> None:
    for props in schedules(template):
        assert props["ScheduleExpressionTimezone"] == TIMEZONE
        assert props["ScheduleExpression"].endswith(" ? * MON-FRI *)")
        assert props["GroupName"] == "auratrade-paper"
        assert props["State"] == "ENABLED"
        assert props["FlexibleTimeWindow"] == {"Mode": "OFF"}
        assert props["Target"]["RetryPolicy"] == {
            "MaximumEventAgeInSeconds": 300,
            "MaximumRetryAttempts": 2,
        }


def fired(expression: str) -> set[time]:
    minutes, hours = expression.removeprefix("cron(").split()[:2]
    first, _, last = hours.partition("-")
    return {
        time(h, int(m))
        for h in range(int(first), int(last or first) + 1)
        for m in minutes.split(",")
    }


def test_the_schedules_are_exactly_the_slots(template: Any) -> None:
    by_job: dict[str, set[time]] = {}
    for props in schedules(template):
        job = props["Name"].removeprefix("auratrade-paper-").rsplit("-", 1)[0]
        assert props["Target"]["Arn"] == {"Fn::GetAtt": [logical_id(template, job), "Arn"]}
        by_job.setdefault(job, set()).update(fired(props["ScheduleExpression"]))
    expected: dict[str, set[time]] = {}
    for slot in schedule.skeleton_slots(load_risk_config().jobs.chain_wait_minutes):
        expected.setdefault(slot.job, set()).update(slot.times())
    assert by_job == expected


@pytest.mark.parametrize(
    ("times", "expressions"),
    [
        ([time(9, 20)], ["cron(20 9 ? * MON-FRI *)"]),
        ([time(9, 20), time(9, 31)], ["cron(20,31 9 ? * MON-FRI *)"]),
        (
            [time(17, 0), time(17, 10), time(17, 20), time(17, 30)],
            ["cron(0,10,20,30 17 ? * MON-FRI *)"],
        ),
        (
            [time(17, 50), time(18, 0), time(18, 10), time(18, 20)],
            ["cron(50 17 ? * MON-FRI *)", "cron(0,10,20 18 ? * MON-FRI *)"],
        ),
        (
            [time(h, m) for h in (7, 8, 9) for m in (0, 15, 30, 45)] + [time(10, 0)],
            ["cron(0,15,30,45 7-9 ? * MON-FRI *)", "cron(0 10 ? * MON-FRI *)"],
        ),
        (
            [time(9, 0), time(11, 0)],
            ["cron(0 9 ? * MON-FRI *)", "cron(0 11 ? * MON-FRI *)"],
        ),
    ],
)
def test_cron_expressions(times: list[time], expressions: list[str]) -> None:
    assert cron_expressions(times) == expressions
    assert set().union(*(fired(e) for e in expressions)) == set(times)


def test_the_scheduler_role_is_bound_to_the_account(template: Any) -> None:
    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": Match.object_like(
                {
                    "Statement": [
                        Match.object_like(
                            {
                                "Principal": {"Service": "scheduler.amazonaws.com"},
                                "Condition": {
                                    "StringEquals": {"aws:SourceAccount": "123456789012"}
                                },
                            }
                        )
                    ]
                }
            )
        },
    )
