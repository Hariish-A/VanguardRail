"""The Guardrail control-plane stack.

Every resource here sits inside an AWS always-free allowance. Two choices are worth
explaining, because they look unusual next to a textbook serverless stack:

**Lambda Function URL instead of API Gateway.** API Gateway's 1M requests/month is a
12-month introductory offer, after which HTTP APIs cost $1.00/million. A Function URL
carries no per-request charge at all, ever. To avoid the usual downside of a Function
URL -- a publicly reachable endpoint with no edge in front of it -- the URL uses IAM
auth and is fronted by CloudFront with an Origin Access Control, so the raw
`*.lambda-url.*` address returns 403 to everyone except CloudFront. That is a tighter
posture than a public API Gateway stage, not a looser one.

**Zip + layer instead of a container image.** Container-image Lambdas require a private
ECR repository, and private ECR is 500 MB free for 12 months only, then $0.10/GB-month.
The dependency set here is roughly 60-80 MB, comfortably inside the 250 MB unzipped
limit, so the 10 GB image ceiling buys nothing and would cost money. The repository
still ships a Dockerfile -- used for local development, as the CI test runner, and to
prove the service runs unchanged on any container host.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import aws_cdk as cdk
from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

# Everything the service needs at runtime. Deliberately excludes boto3, which the
# Lambda runtime already provides -- bundling it would waste ~50 MB of the 250 MB limit.
RUNTIME_DEPENDENCIES = [
    "fastapi>=0.115",
    "mangum>=0.19",
    "pydantic>=2.9",
    "pydantic-settings>=2.5",
    "aws-lambda-powertools>=3.0",
]

# Build steps for the dependency layer, run inside the official Lambda arm64 image so
# compiled wheels (pydantic-core above all) match the runtime platform exactly.
#
# Each requirement is single-quoted: unquoted `>=` is a shell redirect, so
# `pip install fastapi>=0.115` silently installs the wrong thing and writes a file
# named `=0.115`.
_LAYER_BUILD = [
    "pip install --quiet --upgrade pip",
    "pip install --quiet "
    + " ".join(f"'{dep}'" for dep in RUNTIME_DEPENDENCIES)
    + " --target /asset-output/python",
    "find /asset-output -name __pycache__ -type d -prune -exec rm -rf {} + || true",
    "find /asset-output -name tests -type d -prune -exec rm -rf {} + || true",
]

# Build steps for the function itself: our own first-party packages, nothing more.
_CODE_BUILD = [
    "cp -r guardrail-core/src/guardrail_core /asset-output/",
    "cp -r guardrail-service/src/guardrail_service /asset-output/",
    "find /asset-output -name __pycache__ -type d -prune -exec rm -rf {} + || true",
]

# Set to skip Docker-based bundling so `cdk synth` works without a running Docker
# daemon. Synth-only: the resulting asset is a placeholder, never deployable.
SKIP_BUNDLING = os.environ.get("GUARDRAIL_SKIP_BUNDLING", "").lower() in {"1", "true", "yes"}

PLACEHOLDER_ASSET = Path(__file__).parent.parent / "placeholder"


class ServiceStack(Stack):
    """Lambda + Function URL + CloudFront, plus the log group that caps log spend."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        stage: str,
        version: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.stage = stage

        log_group = self._create_log_group()
        self.function = self._create_function(stage=stage, version=version, log_group=log_group)
        self.function_url = self._create_function_url(self.function)
        self.distribution = self._create_distribution(self.function_url)
        # No explicit lambda:InvokeFunctionUrl grant here on purpose:
        # FunctionUrlOrigin.with_origin_access_control() already emits one, scoped to
        # this distribution's ARN and partition-aware. Adding a second is redundant.

        self._create_outputs()

    # ------------------------------------------------------------------
    # Compute
    # ------------------------------------------------------------------
    def _create_log_group(self) -> logs.LogGroup:
        """Seven-day retention, declared here rather than left to Lambda's default.

        Lambda auto-creates log groups with *infinite* retention, which is the easiest
        way to walk past the 5 GB CloudWatch Logs allowance and start paying. Declaring
        the group in CDK is what makes the limit enforceable.
        """
        return logs.LogGroup(
            self,
            "ServiceLogGroup",
            log_group_name=f"/aws/lambda/guardrail-service-{self.stage}",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

    def _code(self, commands: list[str]) -> lambda_.Code:
        """Build a Lambda asset inside the Lambda arm64 image.

        When GUARDRAIL_SKIP_BUNDLING is set, a placeholder asset is used instead. That
        exists so `cdk synth` -- and therefore template validation and the
        banned-resource scan -- can run on a machine without Docker running. The
        resource *types* in the template are identical either way, which is all the
        cost scan inspects. Never deploy from a skipped-bundling synth; `deploy` in CI
        always runs with Docker available.
        """
        if SKIP_BUNDLING:
            return lambda_.Code.from_asset(str(PLACEHOLDER_ASSET))

        return lambda_.Code.from_asset(
            "../packages",
            bundling=cdk.BundlingOptions(
                image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                platform="linux/arm64",
                command=["bash", "-c", " && ".join(commands)],
            ),
        )

    def _create_function(
        self,
        *,
        stage: str,
        version: str,
        log_group: logs.LogGroup,
    ) -> lambda_.Function:
        """The control plane itself.

        arm64 (Graviton) is both cheaper per GB-second and faster than x86 here, which
        matters because the Lambda free tier is denominated in GB-seconds.
        """
        dependencies_layer = lambda_.LayerVersion(
            self,
            "DependenciesLayer",
            layer_version_name=f"guardrail-deps-{stage}",
            code=self._code(_LAYER_BUILD),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            compatible_architectures=[lambda_.Architecture.ARM_64],
            description="Third-party runtime dependencies for the Guardrail service.",
        )

        return lambda_.Function(
            self,
            "ServiceFunction",
            function_name=f"guardrail-service-{stage}",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="guardrail_service.handler.lambda_handler",
            code=self._code(_CODE_BUILD),
            layers=[dependencies_layer],
            # 512 MB is the balance point: enough CPU that policy evaluation is not
            # throttled, small enough that ~800k invocations/month stay inside the
            # 400,000 GB-second allowance.
            memory_size=512,
            timeout=Duration.seconds(10),
            log_group=log_group,
            environment={
                "GUARDRAIL_STAGE": stage,
                "GUARDRAIL_VERSION": version,
                "GUARDRAIL_LOG_LEVEL": "INFO" if stage == "prod" else "DEBUG",
                "GUARDRAIL_SERVICE_NAME": "guardrail",
                # Powertools reads these directly.
                "POWERTOOLS_SERVICE_NAME": "guardrail",
                "POWERTOOLS_METRICS_NAMESPACE": "guardrail",
                "POWERTOOLS_LOG_LEVEL": "INFO" if stage == "prod" else "DEBUG",
            },
            description=f"Guardrail control plane ({stage}) -- action-layer policy enforcement.",
        )

    # ------------------------------------------------------------------
    # Edge
    # ------------------------------------------------------------------
    def _create_function_url(self, function: lambda_.Function) -> lambda_.FunctionUrl:
        """IAM-authenticated Function URL.

        IAM auth makes the raw URL unusable without SigV4 signing. CloudFront's Origin
        Access Control performs that signing, so CloudFront can reach the function and
        the open internet cannot.
        """
        return function.add_function_url(auth_type=lambda_.FunctionUrlAuthType.AWS_IAM)

    def _create_distribution(self, function_url: lambda_.FunctionUrl) -> cloudfront.Distribution:
        """CloudFront in front of the Function URL.

        Beyond hiding the origin, this is what lets the console and the API share one
        domain in M3 -- no CORS preflight to configure, and one place to attach a custom
        domain later.
        """
        return cloudfront.Distribution(
            self,
            "Distribution",
            comment=f"Guardrail control plane ({self.stage})",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.FunctionUrlOrigin.with_origin_access_control(function_url),
                # An API must never be cached: a stale policy decision is a wrong one.
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                # Forward everything except Host, which must remain the origin's.
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
            # PRICE_CLASS_100 (North America + Europe) is the cheapest tier and keeps
            # egress inside the always-free 1 TB allowance.
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            enable_logging=False,  # Access logs would eat into the S3 free tier.
        )

    # ------------------------------------------------------------------
    # Outputs
    # ------------------------------------------------------------------
    def _create_outputs(self) -> None:
        """Values the CI pipeline and the handoff notes need."""
        cdk.CfnOutput(
            self,
            "BaseUrl",
            value=f"https://{self.distribution.distribution_domain_name}",
            description="Public base URL of the Guardrail control plane.",
            export_name=f"guardrail-base-url-{self.stage}",
        )
        cdk.CfnOutput(
            self,
            "FunctionUrl",
            value=self.function_url.url,
            description="Origin URL. Returns 403 unless called by CloudFront -- that is intended.",
        )
        cdk.CfnOutput(
            self,
            "FunctionName",
            value=self.function.function_name,
            description="Lambda function name, for log tailing.",
        )
