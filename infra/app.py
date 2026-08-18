"""CDK application entrypoint.

Stages are separate stacks rather than API Gateway-style stages, since the service is
fronted by a Lambda Function URL. That is arguably cleaner anyway: `dev` and `prod` are
fully isolated, and destroying one cannot touch the other.

Configuration comes from the environment so the same command works locally and in CI:

    CDK_DEFAULT_ACCOUNT   supplied automatically by the AWS credentials in use
    CDK_DEFAULT_REGION    likewise; defaults to us-east-1
    GUARDRAIL_STAGE       dev | prod          (default: dev)
    GUARDRAIL_VERSION     git SHA             (default: local)
    GUARDRAIL_ALERT_EMAIL address for the cost alarm; the budget stack is skipped without it
"""

from __future__ import annotations

import os

import aws_cdk as cdk
from stacks.budget_stack import BudgetStack
from stacks.service_stack import ServiceStack

# Pinned explicitly so `python app.py` writes somewhere predictable. Without this,
# aws-cdk-lib synthesizes into a temp directory when it is not invoked through the CDK
# CLI, which makes the cost scan in CI silently find nothing to check. The CDK CLI sets
# CDK_OUTDIR itself, so this defers to it when present.
app = cdk.App(outdir=os.environ.get("CDK_OUTDIR", "cdk.out"))

stage = os.environ.get("GUARDRAIL_STAGE", "dev")
version = os.environ.get("GUARDRAIL_VERSION", "local")
alert_email = os.environ.get("GUARDRAIL_ALERT_EMAIL")

if stage not in {"dev", "prod"}:
    raise SystemExit(f"GUARDRAIL_STAGE must be 'dev' or 'prod', got {stage!r}")

# CloudFront-associated resources must be created in us-east-1, so the whole stack
# lives there rather than splitting across regions for no benefit.
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

ServiceStack(
    app,
    f"Guardrail-Service-{stage}",
    stage=stage,
    version=version,
    env=env,
    description=f"Guardrail control plane ({stage}) -- action-layer governance for AI agents.",
)

# Account-wide, so it is only deployed alongside the dev stage and is never torn down
# with a service stack. Skipped when no address is configured, since a budget alarm
# nobody receives is worse than none at all -- it creates false confidence.
if alert_email and stage == "dev":
    BudgetStack(
        app,
        "Guardrail-Budget",
        notification_email=alert_email,
        env=env,
        description="Zero-spend tripwire for the Guardrail account.",
    )

cdk.Tags.of(app).add("project", "guardrail")
cdk.Tags.of(app).add("stage", stage)
cdk.Tags.of(app).add("managed-by", "cdk")

app.synth()
