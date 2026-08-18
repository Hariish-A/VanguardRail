"""Infrastructure assertions on the synthesized template.

Two jobs:

1. Guard the security posture. The Function URL may only be public (`authType: NONE`)
   while CloudFront is absent AND the API carries nothing but health endpoints. Once
   /v1/evaluate exists it will see real tool-call arguments, so the origin must be back
   behind CloudFront's Origin Access Control. That is asserted here rather than left to
   anybody's memory.

2. Guard the cost shape -- arm64, bounded log retention, no surprise resources.
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

INFRA_DIR = Path(__file__).resolve().parents[2] / "infra"
sys.path.insert(0, str(INFRA_DIR))


def _synth(*, enable_cloudfront: bool) -> dict[str, Any]:
    """Synthesize ServiceStack and return its CloudFormation template."""
    import aws_cdk as cdk
    from aws_cdk import assertions

    # Bundling is skipped so these tests need neither Docker nor AWS credentials. Only
    # resource shape is under test, and the placeholder asset produces identical shape.
    os.environ["GUARDRAIL_SKIP_BUNDLING"] = "1"
    os.environ["GUARDRAIL_ENABLE_CLOUDFRONT"] = "true" if enable_cloudfront else "false"

    # Both flags are read at import time, so the module must be reloaded per scenario.
    import stacks.service_stack as service_stack

    importlib.reload(service_stack)

    app = cdk.App()
    stack = service_stack.ServiceStack(app, "TestStack", stage="dev", version="test")

    template: dict[str, Any] = assertions.Template.from_stack(stack).to_json()
    return template


@pytest.fixture(autouse=True)
def _restore_env() -> Iterator[None]:
    yield
    os.environ.pop("GUARDRAIL_ENABLE_CLOUDFRONT", None)
    os.environ.pop("GUARDRAIL_SKIP_BUNDLING", None)


def _resources_of_type(template: dict[str, Any], type_name: str) -> list[dict[str, Any]]:
    return [r for r in template.get("Resources", {}).values() if r.get("Type") == type_name]


# ---------------------------------------------------------------------------
# Security posture
# ---------------------------------------------------------------------------


def test_with_cloudfront_the_origin_requires_iam_auth() -> None:
    """The intended posture: the raw Function URL is unreachable without SigV4."""
    template = _synth(enable_cloudfront=True)

    urls = _resources_of_type(template, "AWS::Lambda::Url")

    assert len(urls) == 1
    assert urls[0]["Properties"]["AuthType"] == "AWS_IAM"


def test_with_cloudfront_a_distribution_and_oac_exist() -> None:
    template = _synth(enable_cloudfront=True)

    assert len(_resources_of_type(template, "AWS::CloudFront::Distribution")) == 1
    assert len(_resources_of_type(template, "AWS::CloudFront::OriginAccessControl")) == 1


def test_with_cloudfront_only_cloudfront_may_invoke_the_url() -> None:
    """The invoke permission must be scoped to the CloudFront service principal."""
    template = _synth(enable_cloudfront=True)

    permissions = _resources_of_type(template, "AWS::Lambda::Permission")

    assert permissions, "expected an invoke permission for CloudFront"
    principals = {p["Properties"].get("Principal") for p in permissions}
    assert principals == {"cloudfront.amazonaws.com"}


def test_without_cloudfront_no_distribution_is_created() -> None:
    """The temporary mode used while the AWS account awaits CloudFront verification."""
    template = _synth(enable_cloudfront=False)

    assert _resources_of_type(template, "AWS::CloudFront::Distribution") == []


def test_without_cloudfront_the_url_is_public_because_nothing_can_sign() -> None:
    """Documents the trade-off rather than hiding it.

    IAM auth with no signer in front would make the endpoint callable by nobody, so this
    mode necessarily exposes the Function URL. It is acceptable only while the API is
    health endpoints with no data and no side effects -- which the next test enforces.
    """
    template = _synth(enable_cloudfront=False)

    urls = _resources_of_type(template, "AWS::Lambda::Url")

    assert len(urls) == 1
    assert urls[0]["Properties"]["AuthType"] == "NONE"


def test_public_function_url_is_only_allowed_while_the_api_is_health_only() -> None:
    """The tripwire that prevents shipping a public origin once real data flows.

    When M1 adds /v1/evaluate, this test fails until CloudFront is restored -- turning a
    security regression into a red build instead of something nobody remembers to check.
    """
    from guardrail_service.app import app

    data_paths = [
        path
        for path in app.openapi()["paths"]
        if not path.startswith(("/healthz", "/readyz", "/version", "/docs", "/openapi"))
    ]

    if not data_paths:
        pytest.skip("API is still health-only; the public Function URL mode is acceptable")

    template = _synth(enable_cloudfront=False)
    urls = _resources_of_type(template, "AWS::Lambda::Url")

    pytest.fail(
        "The API now exposes data endpoints "
        f"({', '.join(sorted(data_paths))}), so the origin must sit behind CloudFront. "
        "Deploy with GUARDRAIL_ENABLE_CLOUDFRONT=true (requires AWS account "
        f"verification for CloudFront). Current auth type: {urls[0]['Properties']['AuthType']}."
    )


# ---------------------------------------------------------------------------
# Cost shape
# ---------------------------------------------------------------------------


def test_lambda_is_arm64_and_memory_bounded() -> None:
    """arm64 is cheaper per GB-second, and the free tier is denominated in GB-seconds."""
    template = _synth(enable_cloudfront=False)

    functions = _resources_of_type(template, "AWS::Lambda::Function")

    assert len(functions) == 1
    assert functions[0]["Properties"]["Architectures"] == ["arm64"]
    assert functions[0]["Properties"]["MemorySize"] == 512


def test_log_group_retention_is_bounded() -> None:
    """Lambda's default is infinite retention, which walks past the 5 GB allowance."""
    template = _synth(enable_cloudfront=False)

    groups = _resources_of_type(template, "AWS::Logs::LogGroup")

    assert len(groups) == 1
    assert groups[0]["Properties"]["RetentionInDays"] == 7


def test_no_paid_resources_in_either_mode() -> None:
    """Reuses the CI cost gate, so both edge modes are held to the same standard."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    from check_banned_resources import BANNED_TYPES

    for enable in (True, False):
        template = _synth(enable_cloudfront=enable)
        found = {r.get("Type") for r in template.get("Resources", {}).values()}
        assert not (found & BANNED_TYPES.keys()), f"paid resource with cloudfront={enable}"
