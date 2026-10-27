"""The trading stack for one environment (README §12, task 1.8): the portfolio's jobs as Lambda
functions on one container image, started by EventBridge Scheduler on `jobs.schedule`.

- **Image:** the repo's `Dockerfile`, built and pushed to the CDK asset repository by
  `cdk deploy`. Every function runs the same image with its own handler.
- **Secrets** stay in SSM Parameter Store under `/auratrade/<env>/` (put there by
  `scripts/put_parameters.py`); each function reads them at start (`auratrade.parameters`).
  Nothing secret is in the template.
- **Trading mode** is a deploy-time choice and defaults to `dry_run`: paper orders need
  `-c trading_mode=paper`, and `live` is refused here as it is by the settings (Phase 8).
- **Schedules** fire in `America/New_York` on weekdays; the jobs' calendar guards skip
  holidays. Order jobs keep Lambda's async retries (they are idempotent through
  `client_order_id`); the others don't retry, since the next slot or the chain's fallback
  covers them.
- **Storage:** one private bucket for the nightly backup and the bar retention archive.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import time
from pathlib import Path

from aws_cdk import CfnOutput, Duration, Environment, IgnoreMode, RemovalPolicy, Stack, Tags
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_scheduler as scheduler
from constructs import Construct

from auratrade.config import load_risk_config
from auratrade.jobs import schedule

REPO_ROOT = Path(__file__).resolve().parents[2]
TIMEZONE = "America/New_York"
TRADING_MODES = ("dry_run", "paper")
ENVIRONMENTS = ("paper",)


@dataclass(frozen=True)
class Function:
    job: str
    memory_mb: int
    timeout_s: int
    # Lambda's async retries: on for order jobs (README §5.7, plan 1.8), off otherwise.
    retries: int = 0
    storage: bool = False


# Order jobs may wait for the order lock (`execution.lock_wait_max_seconds`) and for fills, so
# they get Lambda's full 15 minutes; a run cut off mid-sequence is what `stop_guard` repairs.
FUNCTIONS: tuple[Function, ...] = (
    # The decision on E: one Claude call with its tools, bounded by `llm` deadlines.
    Function("portfolio_manager", 1024, 900),
    Function("execute_open", 1024, 900, retries=2),
    Function("execute_close", 1024, 900, retries=2),
    Function("post_market", 1024, 900, retries=2, storage=True),
    Function("stop_guard", 1024, 300),
    Function("ingest", 1024, 900, storage=True),
    # The twin, the book and SPY from the trial's start, on the funds' daily bars.
    Function("simulate", 1024, 600),
    Function("regime", 1024, 300),
    Function("allocate", 1024, 600),
    Function("watchdog", 512, 120),
)


def cron_expressions(times: Sequence[time]) -> list[str]:
    """Weekday EventBridge Scheduler cron expressions that fire at exactly `times`: one per run
    of consecutive hours sharing the same minutes."""
    by_hour: dict[int, list[int]] = defaultdict(list)
    for t in times:
        by_hour[t.hour].append(t.minute)
    groups: list[tuple[list[int], list[int]]] = []
    for hour in sorted(by_hour):
        minutes = sorted(set(by_hour[hour]))
        if groups and groups[-1][1] == minutes and groups[-1][0][-1] == hour - 1:
            groups[-1][0].append(hour)
        else:
            groups.append(([hour], minutes))
    out = []
    for hours, minutes in groups:
        span = str(hours[0]) if len(hours) == 1 else f"{hours[0]}-{hours[-1]}"
        out.append(f"cron({','.join(map(str, minutes))} {span} ? * MON-FRI *)")
    return out


def function_name(env_name: str, job: str) -> str:
    """Matches `Settings.job_function_template`, which chained jobs use to invoke the next."""
    return f"auratrade-{env_name}-{job}"


class TradingStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: str,
        trading_mode: str = "dry_run",
        env: Environment | None = None,
    ) -> None:
        super().__init__(scope, construct_id, env=env)
        if env_name not in ENVIRONMENTS:
            raise ValueError(f"unknown environment {env_name!r}; expected one of {ENVIRONMENTS}")
        if trading_mode not in TRADING_MODES:
            raise ValueError(
                f"trading_mode {trading_mode!r} is refused; expected one of {TRADING_MODES}"
            )
        Tags.of(self).add("project", "auratrade")
        Tags.of(self).add("environment", env_name)

        bucket = s3.Bucket(
            self,
            "Storage",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            # The backups and the archived bars: never deleted with the stack.
            removal_policy=RemovalPolicy.RETAIN,
        )
        group = scheduler.CfnScheduleGroup(self, "Schedules", name=f"auratrade-{env_name}")
        parameter_path = f"/auratrade/{env_name}/"
        environment = {
            "ENVIRONMENT": env_name,
            "TRADING_MODE": trading_mode,
            "SSM_PARAMETER_PATH": parameter_path,
            "STORAGE_BACKEND": "s3",
            "STORAGE_S3_BUCKET": bucket.bucket_name,
            "SCHEDULE_GROUP": group.name or "",
        }
        # GetParametersByPath is authorized against the path itself, trailing slash included
        # (`parameter/auratrade/paper/`); without it every call is denied.
        read_parameters = iam.PolicyStatement(
            actions=["ssm:GetParametersByPath"],
            resources=[
                self.format_arn(
                    service="ssm", resource="parameter", resource_name=parameter_path.lstrip("/")
                )
            ],
        )

        functions: dict[str, lambda_.Function] = {}
        for spec in FUNCTIONS:
            fn = lambda_.DockerImageFunction(
                self,
                f"Job-{spec.job}",
                function_name=function_name(env_name, spec.job),
                code=lambda_.DockerImageCode.from_image_asset(
                    str(REPO_ROOT),
                    cmd=[f"auratrade.jobs.{spec.job}.handler"],
                    platform=ecr_assets.Platform.LINUX_AMD64,
                    ignore_mode=IgnoreMode.DOCKER,  # the .dockerignore allow-list
                ),
                architecture=lambda_.Architecture.X86_64,
                memory_size=spec.memory_mb,
                timeout=Duration.seconds(spec.timeout_s),
                retry_attempts=spec.retries,
                max_event_age=Duration.hours(1),
                environment=environment,
                log_group=logs.LogGroup(
                    self,
                    f"Logs-{spec.job}",
                    log_group_name=f"/aws/lambda/{function_name(env_name, spec.job)}",
                    retention=logs.RetentionDays.ONE_MONTH,
                    removal_policy=RemovalPolicy.DESTROY,
                ),
            )
            fn.add_to_role_policy(read_parameters)
            if spec.storage:
                bucket.grant_read_write(fn)
            functions[spec.job] = fn

        # Each evening job starts its successor (an async invoke); nothing else invokes.
        for link in schedule.EVENING_CHAIN:
            nxt = schedule.successor(link.job)
            if nxt is not None:
                functions[nxt].grant_invoke(functions[link.job])
        functions["watchdog"].add_to_role_policy(
            iam.PolicyStatement(
                actions=["scheduler:ListSchedules"],
                resources=["*"],  # ListSchedules supports no resource-level permissions
            )
        )

        role = iam.Role(
            self,
            "SchedulerRole",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
        )
        for target in functions.values():
            target.grant_invoke(role)

        chain_wait = load_risk_config().jobs.chain_wait_minutes
        counts: dict[str, int] = defaultdict(int)
        for slot in schedule.weekday_slots(chain_wait):
            for expression in cron_expressions(slot.times()):
                counts[slot.job] += 1
                name = f"{slot.job}-{counts[slot.job]}"
                cfn = scheduler.CfnSchedule(
                    self,
                    f"Schedule-{name}",
                    name=f"auratrade-{env_name}-{name}",
                    group_name=group.name,
                    schedule_expression=expression,
                    schedule_expression_timezone=TIMEZONE,
                    flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(
                        mode="OFF"
                    ),
                    target=scheduler.CfnSchedule.TargetProperty(
                        arn=functions[slot.job].function_arn,
                        role_arn=role.role_arn,
                        input=json.dumps({}),
                        # Only for failures to start the function; a late run is worse than
                        # none for the morning jobs, and the next slot follows anyway.
                        retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(
                            maximum_event_age_in_seconds=300, maximum_retry_attempts=2
                        ),
                    ),
                    state="ENABLED",
                )
                cfn.add_resource_dependency(group)

        CfnOutput(self, "StorageBucket", value=bucket.bucket_name)
        CfnOutput(self, "ScheduleGroup", value=group.name or "")
        CfnOutput(self, "ParameterPath", value=parameter_path)
        CfnOutput(self, "TradingMode", value=trading_mode)
