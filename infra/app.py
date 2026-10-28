"""CDK entrypoint. From infra/: `npx aws-cdk synth`, `npx aws-cdk deploy AuraTradePublicSite`,
`npx aws-cdk deploy AuraTradePaper -c trading_mode=paper`, `npx aws-cdk deploy AuraTradeMcp`."""

import os

import aws_cdk as cdk
from stacks.mcp import McpStack
from stacks.public_site import PublicSiteStack
from stacks.trading import TradingStack

app = cdk.App()
env = cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region="us-east-1")

# Optional custom domain: `-c site_domain=example.com -c site_hosted_zone_id=Z123...`.
# Without them the site is served on its *.cloudfront.net address.
site = PublicSiteStack(
    app,
    "AuraTradePublicSite",
    domain_name=app.node.try_get_context("site_domain"),
    hosted_zone_id=app.node.try_get_context("site_hosted_zone_id"),
    env=env,
)

# The portfolio's jobs on the `paper` account. Without `-c trading_mode=paper` it deploys as
# `dry_run`. It publishes the dashboard's snapshot to the site's bucket, so the site deploys first.
TradingStack(
    app,
    "AuraTradePaper",
    env_name="paper",
    trading_mode=app.node.try_get_context("trading_mode") or "dry_run",
    site_bucket=site.bucket,
    env=env,
)

# The remote MCP server's public tools, for the account's own credentials only (#175).
McpStack(app, "AuraTradeMcp", site_bucket=site.bucket, env=env)

app.synth()
