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

# Bundling scripts, run inside the official Lambda arm64 image so compiled wheels
# (pydantic-core above all) match the runtime platform exactly. Verified: the layer
# yields _pydantic_core.cpython-312-aarch64-linux-gnu.so, not an x86 build.
#
# Three things here are load-bearing, each learned from a failure:
#
# 1. `set -euo pipefail`, and cleanup steps on their own lines. The original joined
#    every step with ` && ` and ended with `|| true`. Because `&&` and `||` are
#    left-associative with equal precedence, a failure in the FIRST command fell
#    through to the trailing `|| true` and the whole script exited 0 with an empty
#    output directory -- surfacing much later as an opaque "BundlingProducedNoOutput".
#    A build script must fail loudly at the step that broke.
#
# 2. No `pip install --upgrade pip`. CDK runs the bundling container as uid 1000, so
#    that step dies with "Permission denied: '/.local'" -- which is exactly what the
#    masked failure above was hiding. The image's pip is fine as shipped.
#
# 3. Every requirement is single-quoted. Unquoted `>=` is a shell redirect, so
#    `pip install fastapi>=0.115` installs an unpinned fastapi and writes a junk file
#    named `=0.115`.
#
# The explicit emptiness check at the end converts CDK's generic bundling error into a
# message that names the actual problem.
_LAYER_BUILD = f"""
set -euo pipefail
export HOME=/tmp
pip install --no-cache-dir {" ".join(f"'{dep}'" for dep in RUNTIME_DEPENDENCIES)} \
    --target /asset-output/python
find /asset-output -name __pycache__ -type d -prune -exec rm -rf {{}} + 2>/dev/null || true
find /asset-output -name tests -type d -prune -exec rm -rf {{}} + 2>/dev/null || true
test -n "$(ls -A /asset-output/python 2>/dev/null)" || {{
    echo "ERROR: dependency layer build produced no output" >&2
    exit 1
}}
"""

# The function itself: our own first-party packages, nothing more.
_CODE_BUILD = """
set -euo pipefail
cp -r guardrail-core/src/guardrail_core /asset-output/
cp -r guardrail-service/src/guardrail_service /asset-output/
find /asset-output -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
test -f /asset-output/guardrail_service/handler.py || {
    echo "ERROR: function build did not produce handler.py" >&2
    exit 1
}
"""

# Set to skip Docker-based bundling so `cdk synth` works without a running Docker
# daemon. Synth-only: the resulting asset is a placeholder, never deployable.
SKIP_BUNDLING = os.environ.get("GUARDRAIL_SKIP_BUNDLING", "").lower() in {"1", "true", "yes"}

PLACEHOLDER_ASSET = Path(__file__).parent.parent / "placeholder"

# The Lambda Function URL is the edge. CloudFront is an OPTIONAL enhancement, off by
# default.
#
# Originally CloudFront was the front door, hiding the origin behind an Origin Access
# Control. Brand-new AWS accounts cannot create distributions at all -- CloudFront
# returns "Your account must be verified before you can add new CloudFront resources",
# which needs an AWS Support case and 24-48 hours. Rather than make the architecture
# depend on a support queue, security moved where it belongs: into the application.
#
# A public HTTPS endpoint whose every request is authenticated is the normal shape of a
# production API -- API Gateway endpoints are public too. Hiding the origin was defence
# in depth, not the actual control. The actual controls are:
#
#   * agents        -> SigV4-signed requests (authType AWS_IAM), or hashed API keys
#   * console users -> Cognito JWT validated in the app (M3)
#   * abuse         -> reserved concurrency + per-tenant token bucket (M5)
#
# Setting GUARDRAIL_ENABLE_CLOUDFRONT=true restores the distribution once an account is
# verified, for edge caching and a custom domain. Nothing else in the system changes:
# BaseUrl is emitted either way, so the SDK and smoke tests never learn which is active.
ENABLE_CLOUDFRONT = os.environ.get("GUARDRAIL_ENABLE_CLOUDFRONT", "false").lower() in {
    "1",
    "true",
    "yes",
}

# Origins allowed to call the API from a browser. The M3 review console is served from
# static hosting on a different origin (GitHub Pages), so the browser sends a preflight;
# Function URLs answer it natively, with no gateway or proxy in between.
#
# Deliberately not "*": credentials are sent with console requests, and a wildcard origin
# with credentials is both rejected by browsers and wrong in principle.
CONSOLE_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("GUARDRAIL_CONSOLE_ORIGINS", "http://localhost:5173").split(",")
    if origin.strip()
]


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

        self.distribution: cloudfront.Distribution | None = None
        if ENABLE_CLOUDFRONT:
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

    def _code(self, script: str) -> lambda_.Code:
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
                command=["bash", "-c", script],
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
        """The public HTTPS edge for the control plane.

        `authType` is NONE because the callers are a browser console (which cannot sign
        SigV4 without an identity pool) and agents running anywhere -- including outside
        AWS. Authentication is enforced per request inside the application: Cognito JWT
        for console users, hashed API keys for agents. That is the same posture as any
        public API endpoint; the URL being reachable is not itself the vulnerability.

        With CloudFront enabled the auth type becomes AWS_IAM, so only the distribution's
        Origin Access Control can reach the origin -- defence in depth on top of the
        application checks, not a replacement for them.

        CORS is configured here rather than in FastAPI middleware: Function URLs answer
        preflight requests themselves, so the OPTIONS round trip never pays a Lambda cold
        start, and a misconfigured app can't accidentally widen the policy.
        """
        if ENABLE_CLOUDFRONT:
            return function.add_function_url(auth_type=lambda_.FunctionUrlAuthType.AWS_IAM)

        return function.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.NONE,
            cors=lambda_.FunctionUrlCorsOptions(
                allowed_origins=CONSOLE_ORIGINS,
                # OPTIONS is deliberately absent: Function URLs answer preflight
                # themselves and reject OPTIONS as an enum value here.
                allowed_methods=[lambda_.HttpMethod.GET, lambda_.HttpMethod.POST],
                allowed_headers=["content-type", "authorization", "x-api-key", "x-request-id"],
                # Lets the SDK read back the correlation id it was assigned.
                exposed_headers=["x-request-id"],
                allow_credentials=True,
                max_age=Duration.hours(1),
            ),
        )

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
        """Values the CI pipeline and the handoff notes need.

        `BaseUrl` is always the address callers should use, whichever edge is deployed,
        so smoke tests and the SDK never need to know which mode is active.
        """
        if self.distribution is not None:
            base_url = f"https://{self.distribution.distribution_domain_name}"
            edge = "cloudfront"
        else:
            # Function URLs carry a trailing slash; strip it so callers can join paths
            # without producing a double slash.
            base_url = self.function_url.url.rstrip("/")
            edge = "function-url-direct"

        cdk.CfnOutput(
            self,
            "BaseUrl",
            value=base_url,
            description="Public base URL of the Guardrail control plane.",
            export_name=f"guardrail-base-url-{self.stage}",
        )
        cdk.CfnOutput(
            self,
            "EdgeMode",
            value=edge,
            description=(
                "cloudfront = intended posture, origin hidden behind OAC. "
                "function-url-direct = temporary, CloudFront unavailable "
                "(account not yet verified). Must be cloudfront before M1 ships data."
            ),
        )
        cdk.CfnOutput(
            self,
            "FunctionUrl",
            value=self.function_url.url,
            description=(
                "Lambda origin URL. With CloudFront it returns 403 to direct callers, "
                "which is intended."
            ),
        )
        cdk.CfnOutput(
            self,
            "FunctionName",
            value=self.function.function_name,
            description="Lambda function name, for log tailing.",
        )
