"""The demo agent, hosted on AWS and governed by the AWS control plane.

Deliberately a **separate stack** from the service. Two reasons, and the first is the one
that matters:

1. A failure building or deploying a demo component must never put the control plane at
   risk. In one stack, a broken agent build fails the whole deploy.
2. `cdk destroy` on the agent cannot touch the guardrail, its audit table, or its policy
   history.

## Cost

One more Lambda, sharing the account's always-free 1M requests and 400,000 GB-seconds.
A run at 512 MB for ~20 seconds costs about 10 GB-seconds, so the allowance covers tens of
thousands of demos. No new table, no new index, no new custom metric.

## The one real trade-off

The account's total `ConcurrentExecutions` quota is **10**, and per-function reservation is
impossible below that (see the service stack). So this function shares the pool with the
control plane: a burst of agent runs could in principle crowd out evaluations. Negligible
at demo volume, and named rather than hidden -- it is exactly the isolation that reserved
concurrency would have provided.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import aws_cdk as cdk
from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

# httpx for both the LLM call and the guardrail call; pydantic for the SDK's typed
# decision models. Deliberately nothing else -- no FastAPI, no boto3 (the runtime
# provides it, and this function touches no AWS service directly anyway).
AGENT_DEPENDENCIES = ["httpx>=0.27", "pydantic>=2.9"]

_LAYER_BUILD = f"""
set -euo pipefail
export HOME=/tmp
pip install --no-cache-dir {" ".join(f"'{d}'" for d in AGENT_DEPENDENCIES)} \
    --target /asset-output/python
find /asset-output -name __pycache__ -type d -prune -exec rm -rf {{}} + 2>/dev/null || true
test -n "$(ls -A /asset-output/python 2>/dev/null)" || {{
    echo "ERROR: agent dependency layer produced no output" >&2
    exit 1
}}
"""

# The agent lives in apps/ and the SDK in packages/, so the asset root is the repo root.
# Each `cp` is asserted afterwards: an empty or partial copy otherwise surfaces much later
# as an opaque ImportError inside Lambda.
_CODE_BUILD = """
set -euo pipefail
cp -r packages/guardrail-core/src/guardrail_core /asset-output/
cp -r packages/guardrail-sdk/src/guardrail_sdk /asset-output/
cp -r apps/demo-agent/src/demo_agent /asset-output/
find /asset-output -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
for module in guardrail_core guardrail_sdk demo_agent; do
    test -f "/asset-output/$module/__init__.py" || {
        echo "ERROR: $module missing from the agent bundle" >&2
        exit 1
    }
done
test -f /asset-output/demo_agent/handler.py || {
    echo "ERROR: agent handler.py missing from the bundle" >&2
    exit 1
}
"""

SKIP_BUNDLING = os.environ.get("GUARDRAIL_SKIP_BUNDLING", "").lower() in {"1", "true", "yes"}
PLACEHOLDER_ASSET = Path(__file__).parent.parent / "placeholder"

# Everything that must not travel into the Docker build context. `.venv` alone is
# hundreds of megabytes and would make every synth crawl.
_ASSET_EXCLUDES = [
    ".venv",
    ".git",
    ".github",
    "node_modules",
    "infra/cdk.out",
    "**/__pycache__",
    "**/.pytest_cache",
    "**/.mypy_cache",
    "**/.ruff_cache",
    "reports",
    "docs",
    "*.md",
    ".env",
]


class AgentStack(Stack):
    """A governed agent running on Lambda."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        stage: str,
        version: str,
        guardrail_base_url: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.stage = stage

        log_group = logs.LogGroup(
            self,
            "AgentLogGroup",
            log_group_name=f"/aws/lambda/guardrail-agent-{stage}",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.function = self._create_function(
            stage=stage,
            version=version,
            guardrail_base_url=guardrail_base_url,
            log_group=log_group,
        )

        # No IAM grants at all. The agent talks to the guardrail over HTTPS with an API
        # key, exactly as any third-party agent would -- which is the point. Giving it
        # direct table access would make this a privileged insider rather than a
        # demonstration that an ordinary AWS-hosted agent can be governed.

        self.function_url = self.function.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.NONE,
        )

        self._create_outputs()

    # ------------------------------------------------------------------
    def _code(self, script: str) -> lambda_.Code:
        if SKIP_BUNDLING:
            return lambda_.Code.from_asset(str(PLACEHOLDER_ASSET))

        return lambda_.Code.from_asset(
            "..",
            exclude=_ASSET_EXCLUDES,
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
        guardrail_base_url: str,
        log_group: logs.LogGroup,
    ) -> lambda_.Function:
        dependencies_layer = lambda_.LayerVersion(
            self,
            "AgentDependenciesLayer",
            layer_version_name=f"guardrail-agent-deps-{stage}",
            code=self._code(_LAYER_BUILD),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            compatible_architectures=[lambda_.Architecture.ARM_64],
            description="httpx and pydantic for the governed demo agent.",
        )

        return lambda_.Function(
            self,
            "AgentFunction",
            function_name=f"guardrail-agent-{stage}",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="demo_agent.handler.lambda_handler",
            code=self._code(_CODE_BUILD),
            layers=[dependencies_layer],
            memory_size=512,
            # Generous compared with the control plane's 10 seconds, because this waits
            # on hosted inference across several turns. The guardrail's own evaluation
            # stays in single-digit milliseconds regardless -- the decision path never
            # calls a model.
            timeout=Duration.seconds(120),
            log_group=log_group,
            environment={
                # The agent reaches the control plane over public HTTPS with an API key,
                # exactly as an agent running anywhere else would.
                "GUARDRAIL_BASE_URL": guardrail_base_url,
                "GUARDRAIL_API_KEY": os.environ.get("GUARDRAIL_AGENT_GUARDRAIL_KEY", ""),
                # Only a digest is deployed, so the function's configuration discloses no
                # usable credential.
                "GUARDRAIL_AGENT_KEY_SHA256": os.environ.get("GUARDRAIL_AGENT_KEY_SHA256", ""),
                # Hosted inference. Ollama stays the default for local runs; switching
                # provider is configuration, never code.
                "GUARDRAIL_LLM_BASE_URL": os.environ.get(
                    "GUARDRAIL_LLM_BASE_URL", "https://api.groq.com/openai/v1"
                ),
                # Verified against the live model list before deploying: `qwen/qwen3-32b`,
                # which the plan named, has been retired by Groq. Both this and
                # openai/gpt-oss-20b were checked to emit correct tool calls.
                "GUARDRAIL_LLM_MODEL": os.environ.get("GUARDRAIL_LLM_MODEL", "qwen/qwen3.6-27b"),
                "GUARDRAIL_LLM_API_KEY": os.environ.get("GROQ_API_KEY", ""),
                "GUARDRAIL_STAGE": stage,
                "GUARDRAIL_VERSION": version,
            },
            description=(
                f"Guardrail demo agent ({stage}) -- an AWS-hosted agent governed by the "
                "AWS-hosted control plane."
            ),
        )

    def _create_outputs(self) -> None:
        cdk.CfnOutput(
            self,
            "AgentUrl",
            value=self.function_url.url.rstrip("/"),
            description="POST a task here to run a governed agent entirely inside AWS.",
            export_name=f"guardrail-agent-url-{self.stage}",
        )
        cdk.CfnOutput(
            self,
            "AgentFunctionName",
            value=self.function.function_name,
            description="Lambda function name, for log tailing.",
        )
