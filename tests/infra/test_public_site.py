"""Synthesizes the public-site stack offline (no AWS calls) and checks its safety properties."""

import re

import pytest

cdk = pytest.importorskip("aws_cdk")
assertions = pytest.importorskip("aws_cdk.assertions")

from stacks.public_site import (  # noqa: E402
    CONTENT_SECURITY_POLICY,
    DATA_PREFIX,
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
    # script-src falls back to 'none' and style-src is 'self' only.
    assert "<script" not in html
    assert "<style" not in html
    assert not re.search(r"\sstyle=", html)
    assert not re.search(r"\son[a-z]+=", html)
    # No third-party assets: every src/href is relative, root-relative or an in-page anchor.
    for url in re.findall(r'(?:src|href)="([^"]*)"', html):
        assert not re.match(r"[a-z]+:|//", url), url


def test_pages_carry_the_disclaimer() -> None:
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert "Not financial advice" in html
    assert "paper" in html.lower()
