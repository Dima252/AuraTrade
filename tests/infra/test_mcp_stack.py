"""Synthesizes the remote MCP server's stack offline (no AWS calls) and checks who can reach it
(README §5.10, #175)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

cdk = pytest.importorskip("aws_cdk")
assertions = pytest.importorskip("aws_cdk.assertions")
s3 = pytest.importorskip("aws_cdk.aws_s3")

from stacks.mcp import FUNCTION_NAME, SNAPSHOT_KEY, McpStack  # noqa: E402

Template = assertions.Template
ENV = cdk.Environment(account="123456789012", region="us-east-1")


def _app() -> tuple[Any, Any]:
    app = cdk.App()
    site = cdk.Stack(app, "Site", env=ENV)
    bucket = s3.Bucket(site, "SiteBucket")
    return app, McpStack(app, "Mcp", site_bucket=bucket, env=ENV)


@pytest.fixture(scope="module")
def template() -> Any:
    _, stack = _app()
    return Template.from_stack(stack)


def test_only_the_accounts_own_credentials(template: Any) -> None:
    # A request without them is refused before the function runs: no stranger's bill, no
    # stranger taking the trading jobs' concurrency.
    template.has_resource_properties("AWS::Lambda::Url", {"AuthType": "AWS_IAM"})
    template.resource_count_is("AWS::Lambda::Permission", 0)
    template.resource_count_is("AWS::CloudFront::Distribution", 0)


def test_the_function(template: Any) -> None:
    [fn] = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"FunctionName": FUNCTION_NAME}}
    ).values()
    props = fn["Properties"]
    assert props["PackageType"] == "Image"
    assert props["Timeout"] <= 60
    assert "ReservedConcurrentExecutions" not in props  # the account's limit is 10
    variable = props["Environment"]["Variables"]["MCP_SNAPSHOT"]["Fn::Join"][1]
    assert variable[0] == "s3://"
    assert variable[-1] == f"/{SNAPSHOT_KEY}"


def test_it_reads_the_snapshot_alone(template: Any) -> None:
    [policy] = template.find_resources("AWS::IAM::Policy").values()
    [statement] = policy["Properties"]["PolicyDocument"]["Statement"]
    assert statement["Action"] == "s3:GetObject"
    assert statement["Resource"]["Fn::Join"][1][-1] == f"/{SNAPSHOT_KEY}"


def test_the_image_is_the_mcp_target() -> None:
    app, _ = _app()
    assets = json.loads((Path(app.synth().directory) / "Mcp.assets.json").read_text())
    [image] = assets["dockerImages"].values()
    assert image["source"]["dockerBuildTarget"] == "mcp"


def test_the_url_names_the_path(template: Any) -> None:
    value = template.find_outputs("McpUrl")["McpUrl"]["Value"]["Fn::Join"][1]
    assert value[-1] == "mcp"
