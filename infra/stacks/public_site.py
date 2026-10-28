"""Public project site: static files in a private S3 bucket, served by CloudFront (README §12),
and the remote MCP server's public tools at `/mcp` (README §5.10, task 6.4 step 2)."""

from pathlib import Path

from aws_cdk import CfnOutput, Duration, Environment, IgnoreMode, RemovalPolicy, Stack
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as targets
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "web"

# The page ships one self-hosted script (the dashboard, reading data/snapshot.json) and no
# inline code or third-party assets, so the policy can deny everything else.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

# The nightly public snapshot `simulate` writes (README §5.8); deploys must not prune it.
DATA_PREFIX = "data/*"
# The snapshot changes once an evening: served fresh within minutes, not a day.
DATA_TTL = Duration.minutes(5)
SNAPSHOT_KEY = "data/snapshot.json"

# The remote MCP server: one function URL behind the distribution. Its URL takes no auth (MCP
# clients don't sign their POSTs, which origin access control requires), so a reserved
# concurrency caps it: Lambda's concurrency is the account's, shared with the trading jobs, and
# a flood of MCP calls must never throttle an order job. It also bounds the bill.
MCP_PATH = "/mcp"
MCP_FUNCTION = "auratrade-public-mcp"
MCP_CONCURRENCY = 2


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

        self.bucket = bucket = s3.Bucket(
            self,
            "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            # Everything in it is rebuilt from git or by the next simulate run.
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

        origin = origins.S3BucketOrigin.with_origin_access_control(bucket)
        data_cache = cloudfront.CachePolicy(
            self,
            "DataCache",
            default_ttl=DATA_TTL,
            max_ttl=DATA_TTL,
            min_ttl=Duration.seconds(0),
            enable_accept_encoding_gzip=True,
            enable_accept_encoding_brotli=True,
        )
        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=headers,
                compress=True,
            ),
            additional_behaviors={
                DATA_PREFIX: cloudfront.BehaviorOptions(
                    origin=origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=data_cache,
                    response_headers_policy=headers,
                    compress=True,
                )
            },
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

        mcp = lambda_.DockerImageFunction(
            self,
            "McpServer",
            function_name=MCP_FUNCTION,
            code=lambda_.DockerImageCode.from_image_asset(
                str(REPO_ROOT),
                target="mcp",
                platform=ecr_assets.Platform.LINUX_AMD64,
                ignore_mode=IgnoreMode.DOCKER,  # the .dockerignore allow-list
            ),
            architecture=lambda_.Architecture.X86_64,
            memory_size=512,
            timeout=Duration.seconds(15),
            reserved_concurrent_executions=MCP_CONCURRENCY,
            environment={"MCP_SNAPSHOT": f"s3://{bucket.bucket_name}/{SNAPSHOT_KEY}"},
            log_group=logs.LogGroup(
                self,
                "McpLogs",
                log_group_name=f"/aws/lambda/{MCP_FUNCTION}",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        # The snapshot alone, not even a listing: the server can show no more than the site (#174).
        mcp.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"], resources=[bucket.arn_for_objects(SNAPSHOT_KEY)]
            )
        )
        mcp_url = mcp.add_function_url(auth_type=lambda_.FunctionUrlAuthType.NONE)
        distribution.add_behavior(
            MCP_PATH,
            origins.FunctionUrlOrigin(mcp_url),
            # A POST can't follow a redirect to https.
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
            cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            # The function URL routes on its own Host.
            origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
            response_headers_policy=headers,
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
        CfnOutput(self, "McpUrl", value=f"https://{site_host}{MCP_PATH}")
