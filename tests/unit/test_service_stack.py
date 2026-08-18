"""Infrastructure assertions on the synthesized template.

Two jobs:

1. Guard the security posture. The Lambda Function URL is intentionally public, so
   authentication has to live in the application. Every route that is not explicitly
   listed as public must declare an auth dependency; the tripwire below fails the build
   otherwise, rather than leaving it to anybody's memory. CloudFront remains available
   as optional defence in depth and is covered separately.

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
    """The default deployment shape: no CloudFront, no dependency on account verification."""
    template = _synth(enable_cloudfront=False)

    assert _resources_of_type(template, "AWS::CloudFront::Distribution") == []


def test_default_edge_is_a_public_function_url_with_cors() -> None:
    """The default posture: public URL, authentication enforced in the application.

    CORS is set on the Function URL so preflight requests are answered at the edge and
    never pay a Lambda cold start.
    """
    template = _synth(enable_cloudfront=False)

    urls = _resources_of_type(template, "AWS::Lambda::Url")

    assert len(urls) == 1
    props = urls[0]["Properties"]
    assert props["AuthType"] == "NONE"

    cors = props["Cors"]
    # OPTIONS must NOT be listed: Function URLs answer preflight themselves and
    # CloudFormation rejects OPTIONS as an AllowMethods enum value.
    assert "OPTIONS" not in cors["AllowMethods"]
    assert set(cors["AllowMethods"]) == {"GET", "POST"}
    assert cors["AllowCredentials"] is True
    # A wildcard origin alongside credentials is rejected by browsers and wrong anyway.
    assert "*" not in cors["AllowOrigins"]


PUBLIC_PATH_PREFIXES = ("/healthz", "/readyz", "/version", "/docs", "/openapi", "/redoc")
"""Routes that are unauthenticated by design: liveness, readiness, and API docs."""


def _data_paths() -> list[str]:
    """Every route that is not intentionally public."""
    from guardrail_service.app import app

    return sorted(
        path for path in app.openapi()["paths"] if not path.startswith(PUBLIC_PATH_PREFIXES)
    )


def test_every_data_endpoint_is_authenticated() -> None:
    """The tripwire: the edge is a public URL, so auth must live in the application.

    Since the origin is deliberately reachable, "is it authenticated?" is the only
    control that matters. This fails the build the moment a route appears that neither
    is explicitly public nor declares an auth dependency -- turning a silent security
    regression into a red build.

    Skips only while the API is health-only, which is true through M0.
    """
    paths = _data_paths()
    if not paths:
        pytest.skip("API is still health-only; there is nothing yet to authenticate")

    from guardrail_service.app import app

    unprotected = []
    for route in app.routes:
        path = getattr(route, "path", None)
        if path is None or path.startswith(PUBLIC_PATH_PREFIXES):
            continue
        # An authenticated route carries at least one security dependency, registered
        # either on the route or on its router.
        dependant = getattr(route, "dependant", None)
        has_auth = bool(dependant and dependant.security_requirements)
        if not has_auth:
            unprotected.append(path)

    assert not unprotected, (
        f"These routes are publicly reachable with no authentication: {unprotected}. "
        "The Lambda Function URL is intentionally public, so every non-health route "
        "must declare an auth dependency (Cognito JWT for console users, hashed API key "
        "for agents). Add one, or list the route in PUBLIC_PATH_PREFIXES if it is "
        "genuinely meant to be open."
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
