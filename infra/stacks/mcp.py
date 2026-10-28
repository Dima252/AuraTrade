"""The remote MCP server (README §5.10, task 6.4 step 2, #175): the public tools on one Lambda
function with a public URL, for any MCP client (claude.ai's connectors, Claude Desktop, Gemini).

It serves only what the site already shows, so it takes no auth; three things bound what a
stranger can cost, and keep it from ever touching the trading jobs:

- **Reserved concurrency 1:** at most one run at a time, which also caps a function URL at 10
  requests a second; it never takes the Lambda concurrency the trading jobs share.
- **A circuit breaker:** an alarm on its invocations sets that concurrency to 0 (every request
  then throttled, neither run nor billed) and alerts on Telegram; re-armed by hand.
- **The snapshot read once a minute per container**, whatever the load (`remote.py`).
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Environment, IgnoreMode, RemovalPolicy, Stack, Tags
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cloudwatch_actions
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from constructs import Construct

from stacks.trading import REPO_ROOT

FUNCTION_NAME = "auratrade-mcp"
BREAKER_NAME = "auratrade-mcp-breaker"
SNAPSHOT_KEY = "data/snapshot.json"
MCP_PATH = "mcp"
CONCURRENCY = 1
# A conversation makes tens of calls; a thousand in an hour is a flood. Below it, a month of
# calls stays inside Lambda's free tier.
BREAKER_CALLS_PER_HOUR = 1000
# The Telegram bot the trading stack alerts with: the breaker reads its two parameters alone.
ALERT_PARAMETER_PATH = "/auratrade/paper/"
ALERT_PARAMETERS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


class McpStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        site_bucket: s3.IBucket,
        capped: bool = True,
        env: Environment | None = None,
    ) -> None:
        super().__init__(scope, construct_id, env=env)
        Tags.of(self).add("project", "auratrade")

        def image(handler: str) -> lambda_.DockerImageCode:
            return lambda_.DockerImageCode.from_image_asset(
                str(REPO_ROOT),
                target="mcp",
                cmd=[handler],
                platform=ecr_assets.Platform.LINUX_AMD64,
                ignore_mode=IgnoreMode.DOCKER,  # the .dockerignore allow-list
            )

        def log_group(construct_id: str, name: str) -> logs.LogGroup:
            return logs.LogGroup(
                self,
                construct_id,
                log_group_name=f"/aws/lambda/{name}",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            )

        server = lambda_.DockerImageFunction(
            self,
            "Server",
            function_name=FUNCTION_NAME,
            code=image("auratrade.mcp_server.remote.handler"),
            architecture=lambda_.Architecture.X86_64,
            memory_size=512,
            timeout=Duration.seconds(30),
            # Uncapped only until the account's limit is above 10 (Lambda keeps 10 unreserved, so
            # reserving any is refused below that); the breaker still bounds it meanwhile.
            reserved_concurrent_executions=CONCURRENCY if capped else None,
            environment={"MCP_SNAPSHOT": f"s3://{site_bucket.bucket_name}/{SNAPSHOT_KEY}"},
            log_group=log_group("Logs", FUNCTION_NAME),
        )
        # The snapshot alone, not even a listing: the server can show no more than the site (#174).
        server.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"], resources=[site_bucket.arn_for_objects(SNAPSHOT_KEY)]
            )
        )
        url = server.add_function_url(auth_type=lambda_.FunctionUrlAuthType.NONE)

        breaker = lambda_.DockerImageFunction(
            self,
            "Breaker",
            function_name=BREAKER_NAME,
            code=image("auratrade.mcp_breaker.handler"),
            architecture=lambda_.Architecture.X86_64,
            memory_size=512,
            timeout=Duration.seconds(60),
            environment={
                "MCP_FUNCTION": FUNCTION_NAME,
                "ALERT_PARAMETER_PATH": ALERT_PARAMETER_PATH,
            },
            log_group=log_group("BreakerLogs", BREAKER_NAME),
        )
        breaker.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lambda:PutFunctionConcurrency"], resources=[server.function_arn]
            )
        )
        breaker.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameters"],
                resources=[
                    self.format_arn(
                        service="ssm",
                        resource="parameter",
                        resource_name=f"{ALERT_PARAMETER_PATH.strip('/')}/{name}",
                    )
                    for name in ALERT_PARAMETERS
                ],
            )
        )
        flood = cloudwatch.Alarm(
            self,
            "Flood",
            alarm_name=f"{FUNCTION_NAME}-flood",
            alarm_description="Switches the public MCP server off (reserved concurrency 0)",
            metric=server.metric_invocations(period=Duration.hours(1), statistic="Sum"),
            threshold=BREAKER_CALLS_PER_HOUR,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        flood.add_alarm_action(cloudwatch_actions.LambdaAction(breaker))

        CfnOutput(self, "McpUrl", value=f"{url.url}{MCP_PATH}")
