"""Synthesizes the remote MCP server's stack offline (no AWS calls) and checks what bounds it
(README §5.10, #175)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

cdk = pytest.importorskip("aws_cdk")
assertions = pytest.importorskip("aws_cdk.assertions")
s3 = pytest.importorskip("aws_cdk.aws_s3")

from stacks.mcp import (  # noqa: E402
    BREAKER_CALLS_PER_HOUR,
    BREAKER_NAME,
    CONCURRENCY,
    FUNCTION_NAME,
    SNAPSHOT_KEY,
    McpStack,
)

Template = assertions.Template
Match = assertions.Match
ENV = cdk.Environment(account="123456789012", region="us-east-1")


def _app(**kwargs: Any) -> tuple[Any, Any]:
    app = cdk.App()
    site = cdk.Stack(app, "Site", env=ENV)
    bucket = s3.Bucket(site, "SiteBucket")
    return app, McpStack(app, "Mcp", site_bucket=bucket, env=ENV, **kwargs)


@pytest.fixture(scope="module")
def template() -> Any:
    _, stack = _app()
    return Template.from_stack(stack)


def _function(template: Any, name: str) -> dict[str, Any]:
    [fn] = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"FunctionName": name}}
    ).values()
    return dict(fn["Properties"])


def test_a_public_url_capped_at_one_run(template: Any) -> None:
    # Public: any MCP client, no auth (it serves only what the site shows). One run at a time,
    # which caps a function URL at 10 requests a second and leaves the trading jobs' concurrency.
    template.has_resource_properties("AWS::Lambda::Url", {"AuthType": "NONE"})
    props = _function(template, FUNCTION_NAME)
    assert props["ReservedConcurrentExecutions"] == CONCURRENCY == 1
    assert props["PackageType"] == "Image"
    assert props["ImageConfig"]["Command"] == ["auratrade.mcp_server.remote.handler"]
    assert props["Timeout"] <= 60
    variable = props["Environment"]["Variables"]["MCP_SNAPSHOT"]["Fn::Join"][1]
    assert variable[0] == "s3://"
    assert variable[-1] == f"/{SNAPSHOT_KEY}"
    template.resource_count_is("AWS::CloudFront::Distribution", 0)


def test_the_breaker_switches_the_server_off(template: Any) -> None:
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "MetricName": "Invocations",
            "Namespace": "AWS/Lambda",
            "Statistic": "Sum",
            "Period": 3600,
            "Threshold": BREAKER_CALLS_PER_HOUR,
            "ComparisonOperator": "GreaterThanOrEqualToThreshold",
            "TreatMissingData": "notBreaching",
            "AlarmActions": [Match.object_like({"Fn::GetAtt": Match.any_value()})],
        },
    )
    breaker = _function(template, BREAKER_NAME)
    assert breaker["ImageConfig"]["Command"] == ["auratrade.mcp_breaker.handler"]
    assert breaker["Environment"]["Variables"]["MCP_FUNCTION"] == FUNCTION_NAME
    assert "ReservedConcurrentExecutions" not in breaker
    # The alarm may invoke it; nothing else is granted an invoke.
    template.has_resource_properties(
        "AWS::Lambda::Permission",
        {"Action": "lambda:InvokeFunction", "Principal": "lambda.alarms.cloudwatch.amazonaws.com"},
    )


def _statements(template: Any, role_prefix: str) -> list[dict[str, Any]]:
    policies = template.find_resources("AWS::IAM::Policy")
    [policy] = [p for name, p in policies.items() if name.startswith(role_prefix)]
    return list(policy["Properties"]["PolicyDocument"]["Statement"])


def test_the_breaker_may_only_switch_off_and_read_the_bot(template: Any) -> None:
    off, bot = _statements(template, "Breaker")
    assert off["Action"] == "lambda:PutFunctionConcurrency"
    assert "Fn::GetAtt" in off["Resource"]  # the server's ARN, no wildcard
    assert bot["Action"] == "ssm:GetParameters"
    names = [r["Fn::Join"][1][-1].rsplit(":parameter", 1)[1] for r in bot["Resource"]]
    assert names == ["/auratrade/paper/TELEGRAM_BOT_TOKEN", "/auratrade/paper/TELEGRAM_CHAT_ID"]


def test_the_server_reads_the_snapshot_alone(template: Any) -> None:
    [statement] = _statements(template, "Server")
    assert statement["Action"] == "s3:GetObject"
    assert statement["Resource"]["Fn::Join"][1][-1] == f"/{SNAPSHOT_KEY}"


def test_the_image_is_the_mcp_target() -> None:
    app, _ = _app()
    assets = json.loads((Path(app.synth().directory) / "Mcp.assets.json").read_text())
    # One image for the server and the breaker.
    [image] = assets["dockerImages"].values()
    assert image["source"]["dockerBuildTarget"] == "mcp"


def test_the_url_names_the_path(template: Any) -> None:
    value = template.find_outputs("McpUrl")["McpUrl"]["Value"]["Fn::Join"][1]
    assert value[-1] == "mcp"


def test_uncapped_only_on_request_and_still_with_its_breaker() -> None:
    # For an account whose Lambda concurrency limit is still 10: reserving any is refused.
    _, stack = _app(capped=False)
    template = Template.from_stack(stack)
    assert "ReservedConcurrentExecutions" not in _function(template, FUNCTION_NAME)
    template.resource_count_is("AWS::CloudWatch::Alarm", 1)
    assert _function(template, BREAKER_NAME)["Environment"]["Variables"]["MCP_FUNCTION"] == (
        FUNCTION_NAME
    )
