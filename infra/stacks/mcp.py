"""The remote MCP server (README §5.10, task 6.4 step 2, #175): the public tools on one Lambda
function whose URL takes AWS IAM auth, so only the account's own credentials can call it.

MCP clients reach it through AWS's signing proxy (`mcp-proxy-for-aws --service lambda`). A
request without valid credentials is refused by Lambda before the function runs: nobody else
can invoke it, so nobody else can run up its bill or take the trading jobs' concurrency.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Environment, IgnoreMode, RemovalPolicy, Stack, Tags
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from constructs import Construct

from stacks.trading import REPO_ROOT

FUNCTION_NAME = "auratrade-mcp"
SNAPSHOT_KEY = "data/snapshot.json"
MCP_PATH = "mcp"


class McpStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        site_bucket: s3.IBucket,
        env: Environment | None = None,
    ) -> None:
        super().__init__(scope, construct_id, env=env)
        Tags.of(self).add("project", "auratrade")

        fn = lambda_.DockerImageFunction(
            self,
            "Server",
            function_name=FUNCTION_NAME,
            code=lambda_.DockerImageCode.from_image_asset(
                str(REPO_ROOT),
                target="mcp",
                platform=ecr_assets.Platform.LINUX_AMD64,
                ignore_mode=IgnoreMode.DOCKER,  # the .dockerignore allow-list
            ),
            architecture=lambda_.Architecture.X86_64,
            memory_size=512,
            timeout=Duration.seconds(30),
            environment={"MCP_SNAPSHOT": f"s3://{site_bucket.bucket_name}/{SNAPSHOT_KEY}"},
            log_group=logs.LogGroup(
                self,
                "Logs",
                log_group_name=f"/aws/lambda/{FUNCTION_NAME}",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        # The snapshot alone, not even a listing: the server can show no more than the site (#174).
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"], resources=[site_bucket.arn_for_objects(SNAPSHOT_KEY)]
            )
        )
        # No resource policy: only the account's own principals with Lambda permissions.
        url = fn.add_function_url(auth_type=lambda_.FunctionUrlAuthType.AWS_IAM)

        CfnOutput(self, "McpUrl", value=f"{url.url}{MCP_PATH}")
