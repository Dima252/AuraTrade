"""Synthesizes the public-site stack offline (no AWS calls) and checks its safety properties."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

cdk = pytest.importorskip("aws_cdk")
assertions = pytest.importorskip("aws_cdk.assertions")

from stacks.public_site import (  # noqa: E402
    CONTENT_SECURITY_POLICY,
    DATA_PREFIX,
    DATA_TTL,
    MCP_CONCURRENCY,
    MCP_FUNCTION,
    MCP_PATH,
    SNAPSHOT_KEY,
    WEB_DIR,
    PublicSiteStack,
)

Match = assertions.Match
Template = assertions.Template

ENV = cdk.Environment(account="123456789012", region="us-east-1")


def synth(**kwargs: str) -> "assertions.Template":
    app = cdk.App()
    stack = PublicSiteStack(app, "Site", env=ENV, **kwargs)
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template() -> "assertions.Template":
    return synth()


@pytest.fixture(scope="module")
def domain_template() -> "assertions.Template":
    return synth(domain_name="example.com", hosted_zone_id="Z0000000000EXAMPLE")


def test_bucket_is_private_and_encrypted(template: "assertions.Template") -> None:
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
            "BucketEncryption": Match.object_like({}),
        },
    )


def test_bucket_policy_denies_plain_http(template: "assertions.Template") -> None:
    template.has_resource_properties(
        "AWS::S3::BucketPolicy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Effect": "Deny",
                                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                            }
                        )
                    ]
                )
            }
        },
    )


def test_bucket_is_read_only_through_cloudfront(template: "assertions.Template") -> None:
    template.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)
    template.has_resource_properties(
        "AWS::S3::BucketPolicy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Effect": "Allow",
                                "Principal": {"Service": "cloudfront.amazonaws.com"},
                                "Action": "s3:GetObject",
                            }
                        )
                    ]
                )
            }
        },
    )


def test_distribution_forces_https(template: "assertions.Template") -> None:
    template.has_resource_properties(
        "AWS::CloudFront::Distribution",
        {
            "DistributionConfig": Match.object_like(
                {
                    "DefaultRootObject": "index.html",
                    "PriceClass": "PriceClass_100",
                    "DefaultCacheBehavior": Match.object_like(
                        {"ViewerProtocolPolicy": "redirect-to-https", "Compress": True}
                    ),
                    "CustomErrorResponses": [
                        Match.object_like(
                            {
                                "ErrorCode": 403,
                                "ResponseCode": 404,
                                "ResponsePagePath": "/404.html",
                            }
                        )
                    ],
                }
            )
        },
    )


def test_security_headers(template: "assertions.Template") -> None:
    template.has_resource_properties(
        "AWS::CloudFront::ResponseHeadersPolicy",
        {
            "ResponseHeadersPolicyConfig": Match.object_like(
                {
                    "SecurityHeadersConfig": Match.object_like(
                        {
                            "ContentSecurityPolicy": {
                                "ContentSecurityPolicy": CONTENT_SECURITY_POLICY,
                                "Override": True,
                            },
                            "StrictTransportSecurity": Match.object_like(
                                {"AccessControlMaxAgeSec": 31536000}
                            ),
                            "FrameOptions": {"FrameOption": "DENY", "Override": True},
                        }
                    )
                }
            )
        },
    )
    assert "unsafe" not in CONTENT_SECURITY_POLICY
    # Scripts from the site itself only: no inline code, no third parties.
    assert "script-src 'self';" in CONTENT_SECURITY_POLICY


def test_the_snapshot_is_served_fresh(template: "assertions.Template") -> None:
    ttl = int(DATA_TTL.to_seconds())
    assert ttl <= 600
    template.has_resource_properties(
        "AWS::CloudFront::CachePolicy",
        {"CachePolicyConfig": Match.object_like({"DefaultTTL": ttl, "MaxTTL": ttl, "MinTTL": 0})},
    )
    distribution = next(iter(template.find_resources("AWS::CloudFront::Distribution").values()))
    config = distribution["Properties"]["DistributionConfig"]
    [behavior] = [b for b in config["CacheBehaviors"] if b["PathPattern"] == DATA_PREFIX]
    assert behavior["ViewerProtocolPolicy"] == "redirect-to-https"
    assert behavior["TargetOriginId"] == config["DefaultCacheBehavior"]["TargetOriginId"]
    assert (
        behavior["ResponseHeadersPolicyId"]
        == config["DefaultCacheBehavior"]["ResponseHeadersPolicyId"]
    )


def test_the_bucket_is_exposed_for_the_trading_stack() -> None:
    stack = PublicSiteStack(cdk.App(), "Site", env=ENV)
    assert stack.bucket.node.id == "SiteBucket"


def test_deploy_never_prunes_the_data_prefix(template: "assertions.Template") -> None:
    template.has_resource_properties(
        "Custom::CDKBucketDeployment",
        {"Prune": True, "Exclude": [DATA_PREFIX], "DistributionPaths": ["/*"]},
    )


def test_no_domain_means_no_certificate_or_dns(template: "assertions.Template") -> None:
    template.resource_count_is("AWS::CertificateManager::Certificate", 0)
    template.resource_count_is("AWS::Route53::RecordSet", 0)
    distribution = next(iter(template.find_resources("AWS::CloudFront::Distribution").values()))
    assert "Aliases" not in distribution["Properties"]["DistributionConfig"]


def test_custom_domain_gets_certificate_aliases_and_records(
    domain_template: "assertions.Template",
) -> None:
    domain_template.has_resource_properties(
        "AWS::CertificateManager::Certificate",
        {
            "DomainName": "example.com",
            "SubjectAlternativeNames": ["www.example.com"],
            "ValidationMethod": "DNS",
        },
    )
    domain_template.has_resource_properties(
        "AWS::CloudFront::Distribution",
        {
            "DistributionConfig": Match.object_like(
                {
                    "Aliases": ["example.com", "www.example.com"],
                    "ViewerCertificate": Match.object_like(
                        {"MinimumProtocolVersion": "TLSv1.2_2021", "SslSupportMethod": "sni-only"}
                    ),
                }
            )
        },
    )
    # A and AAAA for the apex and for www.
    domain_template.resource_count_is("AWS::Route53::RecordSet", 4)


def test_domain_requires_zone() -> None:
    with pytest.raises(ValueError, match="together"):
        PublicSiteStack(cdk.App(), "Site", env=ENV, domain_name="example.com")


@pytest.mark.parametrize("page", ["index.html", "404.html"])
def test_pages_fit_the_content_security_policy(page: str) -> None:
    html = (WEB_DIR / page).read_text(encoding="utf-8")
    # script-src and style-src are 'self' only: files, never inline code.
    for script in re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.S):
        assert not script.strip()
    assert set(re.findall(r'<script src="([^"]*)"', html)) <= {"dashboard.js"}
    assert html.count("<script") == len(re.findall(r'<script src="', html))
    assert "<style" not in html
    assert not re.search(r"\sstyle=", html)
    assert not re.search(r"\son[a-z]+=", html)
    # No third-party assets: every src/href is relative, root-relative or an in-page anchor.
    for url in re.findall(r'(?:src|href)="([^"]*)"', html):
        assert not re.match(r"[a-z]+:|//", url), url


@pytest.mark.parametrize("page", ["index.html", "404.html"])
def test_pages_carry_the_disclaimer(page: str) -> None:
    html = (WEB_DIR / page).read_text(encoding="utf-8")
    assert "Not financial advice" in html
    assert "paper" in html.lower()


def test_the_dashboard_script_builds_no_markup() -> None:
    js = (WEB_DIR / "dashboard.js").read_text(encoding="utf-8")
    # Snapshot text reaches the page as text: no HTML parsing, no eval, nothing remote.
    for banned in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    ):
        assert banned not in js, banned
    assert re.findall(r'fetch\("([^"]*)"', js) == ["data/snapshot.json"]
    assert "http" not in js.replace("http://www.w3.org/2000/svg", "")


def _mcp_behavior(template: "assertions.Template") -> dict[str, Any]:
    distribution = next(iter(template.find_resources("AWS::CloudFront::Distribution").values()))
    behaviors = distribution["Properties"]["DistributionConfig"]["CacheBehaviors"]
    [behavior] = [b for b in behaviors if b["PathPattern"] == MCP_PATH]
    return dict(behavior)


def test_the_mcp_server_is_capped(template: "assertions.Template") -> None:
    # The account's concurrency is shared with the trading jobs: the cap keeps a flood of MCP
    # calls from throttling an order job.
    assert 1 <= MCP_CONCURRENCY <= 5
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": MCP_FUNCTION,
            "PackageType": "Image",
            "ReservedConcurrentExecutions": MCP_CONCURRENCY,
            "Timeout": Match.any_value(),
            "Environment": {
                "Variables": {
                    "MCP_SNAPSHOT": {
                        "Fn::Join": ["", ["s3://", {"Ref": Match.any_value()}, f"/{SNAPSHOT_KEY}"]]
                    }
                }
            },
        },
    )
    [fn] = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"FunctionName": MCP_FUNCTION}}
    ).values()
    assert fn["Properties"]["Timeout"] <= 30  # under CloudFront's origin timeout


def test_the_mcp_image_is_its_own_target() -> None:
    app = cdk.App()
    PublicSiteStack(app, "Site", env=ENV)
    assets = json.loads((Path(app.synth().directory) / "Site.assets.json").read_text())
    [image] = assets["dockerImages"].values()
    assert image["source"]["dockerBuildTarget"] == "mcp"


def test_the_mcp_server_reads_the_snapshot_alone(template: "assertions.Template") -> None:
    policies = template.find_resources("AWS::IAM::Policy")
    [policy] = [p for name, p in policies.items() if name.startswith("McpServer")]
    [statement] = policy["Properties"]["PolicyDocument"]["Statement"]
    assert statement["Action"] == "s3:GetObject"
    assert statement["Resource"]["Fn::Join"][1][-1] == f"/{SNAPSHOT_KEY}"


def test_the_mcp_url_is_public_behind_the_distribution(template: "assertions.Template") -> None:
    template.has_resource_properties("AWS::Lambda::Url", {"AuthType": "NONE"})
    behavior = _mcp_behavior(template)
    assert behavior["ViewerProtocolPolicy"] == "https-only"
    assert "POST" in behavior["AllowedMethods"]
    # CachingDisabled and AllViewerExceptHostHeader, AWS's managed policies.
    assert behavior["CachePolicyId"] == "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
    assert behavior["OriginRequestPolicyId"] == "b689b0a8-53d0-40ab-baf2-68738e2966ac"
    template.has_output("McpUrl", {"Value": Match.any_value()})
