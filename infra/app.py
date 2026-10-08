"""CDK entrypoint. From infra/: `npx aws-cdk synth`, `npx aws-cdk deploy AuraTradePublicSite`."""

import os

import aws_cdk as cdk
from stacks.public_site import PublicSiteStack

app = cdk.App()

# Optional custom domain: `-c site_domain=example.com -c site_hosted_zone_id=Z123...`.
# Without them the site is served on its *.cloudfront.net address.
PublicSiteStack(
    app,
    "AuraTradePublicSite",
    domain_name=app.node.try_get_context("site_domain"),
    hosted_zone_id=app.node.try_get_context("site_hosted_zone_id"),
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region="us-east-1"),
)

app.synth()
