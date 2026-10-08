"""Public project site: static files in a private S3 bucket, served by CloudFront (README §12)."""

from pathlib import Path

from aws_cdk import CfnOutput, Duration, Environment, RemovalPolicy, Stack
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as targets
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct

WEB_DIR = Path(__file__).resolve().parents[2] / "web"

# The page ships no scripts and no third-party assets, so the policy can deny everything else.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; style-src 'self'; img-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

# Reserved for the nightly public snapshot that post_market will write; deploys must not prune it.
DATA_PREFIX = "data/*"


class PublicSiteStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        domain_name: str | None = None,
        hosted_zone_id: str | None = None,
        env: Environment | None = None,
    ) -> None:
        super().__init__(scope, construct_id, env=env)

        if (domain_name is None) != (hosted_zone_id is None):
            raise ValueError("domain_name and hosted_zone_id must be given together")

        bucket = s3.Bucket(
            self,
            "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            # Everything in it is rebuilt from git or by the next post_market run.
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        headers = cloudfront.ResponseHeadersPolicy(
            self,
            "SecurityHeaders",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=CONTENT_SECURITY_POLICY, override=True
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365),
                    include_subdomains=True,
                    override=True,
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                    override=True,
                ),
            ),
        )

        zone: route53.IHostedZone | None = None
        certificate: acm.ICertificate | None = None
        aliases: list[str] | None = None
        if domain_name is not None and hosted_zone_id is not None:
            zone = route53.HostedZone.from_hosted_zone_attributes(
                self, "Zone", hosted_zone_id=hosted_zone_id, zone_name=domain_name
            )
            aliases = [domain_name, f"www.{domain_name}"]
            # CloudFront only accepts certificates from us-east-1, which is also our region.
            certificate = acm.Certificate(
                self,
                "Certificate",
                domain_name=domain_name,
                subject_alternative_names=aliases[1:],
                validation=acm.CertificateValidation.from_dns(zone),
            )

        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=headers,
                compress=True,
            ),
            default_root_object="index.html",
            # S3 behind origin access control answers 403, not 404, for a missing key.
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=404,
                    response_page_path="/404.html",
                    ttl=Duration.minutes(5),
                )
            ],
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            # Only settable with our own certificate; the *.cloudfront.net one has a fixed policy.
            minimum_protocol_version=(
                cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021 if certificate else None
            ),
            domain_names=aliases,
            certificate=certificate,
        )

        s3deploy.BucketDeployment(
            self,
            "DeploySite",
            sources=[s3deploy.Source.asset(str(WEB_DIR))],
            destination_bucket=bucket,
            exclude=[DATA_PREFIX],
            prune=True,
            distribution=distribution,
            distribution_paths=["/*"],
        )

        if zone is not None and aliases is not None:
            target = route53.RecordTarget.from_alias(targets.CloudFrontTarget(distribution))
            for i, name in enumerate(aliases):
                route53.ARecord(self, f"AliasA{i}", zone=zone, record_name=name, target=target)
                route53.AaaaRecord(
                    self, f"AliasAaaa{i}", zone=zone, record_name=name, target=target
                )

        site_host = domain_name or distribution.distribution_domain_name
        CfnOutput(self, "SiteUrl", value=f"https://{site_host}")
        CfnOutput(self, "BucketName", value=bucket.bucket_name)
