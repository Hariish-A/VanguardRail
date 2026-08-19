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
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

INFRA_DIR = Path(__file__).resolve().parents[2] / "infra"
sys.path.insert(0, str(INFRA_DIR))


def _synth(*, enable_cloudfront: bool, overrides: dict[str, str] | None = None) -> dict[str, Any]:
    """Synthesize ServiceStack and return its CloudFormation template.

    `overrides` sets environment variables the stack reads, for tests that need a
    specific value. Everything else is cleared first -- see below.
    """
    import aws_cdk as cdk
    from aws_cdk import assertions

    # Bundling is skipped so these tests need neither Docker nor AWS credentials. Only
    # resource shape is under test, and the placeholder asset produces identical shape.
    os.environ["GUARDRAIL_SKIP_BUNDLING"] = "1"
    os.environ["GUARDRAIL_ENABLE_CLOUDFRONT"] = "true" if enable_cloudfront else "false"

    # Anything the stack reads from the environment is cleared unless a test sets it
    # deliberately. Without this the template depends on the developer's shell: sourcing
    # .env before running the suite put a real policy-admin allowlist into os.environ,
    # and `test_policy_administration_is_closed_by_default` failed locally while passing
    # in CI. A test whose result moves with ambient state cannot be trusted in either
    # direction -- it can mask a regression as easily as invent one.
    for name in (
        "GUARDRAIL_POLICY_ADMIN_KEY_IDS",
        "GUARDRAIL_POLICY_REFRESH_SECONDS",
        "GUARDRAIL_RESERVED_CONCURRENCY",
        "GUARDRAIL_RATE_LIMIT_PER_MINUTE",
    ):
        os.environ.pop(name, None)
    for name, value in (overrides or {}).items():
        os.environ[name] = value

    # Both flags are read at import time, so the module must be reloaded per scenario.
    import stacks.service_stack as service_stack

    importlib.reload(service_stack)

    app = cdk.App()
    stack = service_stack.ServiceStack(app, "TestStack", stage="dev", version="test")

    template: dict[str, Any] = assertions.Template.from_stack(stack).to_json()
    return template


@pytest.fixture(autouse=True)
def _restore_env() -> Iterator[None]:
    """Leave the process environment as it was found.

    `_synth` deliberately clears variables the stack reads, so anything it removed has to
    be put back -- otherwise these tests would silently change behaviour for every test
    that runs after them.
    """
    watched = (
        "GUARDRAIL_ENABLE_CLOUDFRONT",
        "GUARDRAIL_POLICY_ADMIN_KEY_IDS",
        "GUARDRAIL_POLICY_REFRESH_SECONDS",
        "GUARDRAIL_RESERVED_CONCURRENCY",
        "GUARDRAIL_RATE_LIMIT_PER_MINUTE",
    )
    saved = {name: os.environ.get(name) for name in watched}
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
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


def test_data_paths_discovery_is_not_silently_empty() -> None:
    """Guards the guard.

    The authentication test below iterates documented non-public paths. If that list
    were ever empty it would skip while every endpoint sat wide open, so assert the
    discovery mechanism actually sees the API.
    """
    from guardrail_service.app import app

    documented = set(app.openapi()["paths"])

    assert "/healthz" in documented, f"path discovery is broken; found {documented}"
    assert "/v1/evaluate" in documented, (
        "the evaluate endpoint is missing from the OpenAPI schema, so the "
        f"authentication tripwire would skip. Found: {sorted(documented)}"
    )


def test_every_data_endpoint_rejects_unauthenticated_requests() -> None:
    """The tripwire: the edge is a public URL, so auth must live in the application.

    Deliberately behavioural rather than introspective. An earlier version walked
    `app.routes` looking for security dependencies and found nothing at all, because
    FastAPI wraps included routers in a private container whose `path` is None -- so it
    passed vacuously while /v1/evaluate sat fully exposed. Sending real requests tests
    the property that actually matters and cannot drift with FastAPI internals.
    """
    from fastapi.testclient import TestClient
    from guardrail_service.app import app

    paths = _data_paths()
    assert paths, "no non-public paths found; the tripwire would be vacuous"

    client = TestClient(app)
    spec = app.openapi()["paths"]
    unprotected: list[str] = []

    for path in paths:
        for method in spec[path]:
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            # Path parameters get a placeholder so the request reaches the handler
            # instead of 404-ing before authentication is ever consulted.
            concrete = re.sub(r"\{[^}]+\}", "test-id", path)
            response = client.request(method.upper(), concrete, json={})

            if response.status_code != 401:
                unprotected.append(f"{method.upper()} {path} -> {response.status_code}")

    assert not unprotected, (
        f"These endpoints did not reject an unauthenticated request: {unprotected}. "
        "The Lambda Function URL is intentionally public, so every non-health route must "
        "require an API key. Add the require_api_key dependency, or list the route in "
        "PUBLIC_PATH_PREFIXES if it is genuinely meant to be open."
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


def test_the_service_role_cannot_delete_audit_records() -> None:
    """A governance system whose own role can erase its evidence offers much weaker
    assurance than one that cannot.

    UpdateItem is granted (human-review resolution needs it); DeleteItem never is.
    """
    template = _synth(enable_cloudfront=False)

    actions: set[str] = set()
    for resource in template.get("Resources", {}).values():
        if resource.get("Type") != "AWS::IAM::Policy":
            continue
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]:
            raw = statement.get("Action", [])
            actions.update([raw] if isinstance(raw, str) else raw)

    dynamo = {a for a in actions if a.startswith("dynamodb:")}

    assert "dynamodb:UpdateItem" in dynamo, "resolution needs UpdateItem"
    assert "dynamodb:DeleteItem" not in dynamo, f"role can delete evidence: {dynamo}"
    assert not any(a.endswith("*") for a in dynamo), f"wildcard grant found: {dynamo}"


def test_policy_administration_is_closed_by_default() -> None:
    """The allowlist ships empty, so a deployment cannot accidentally let any agent key
    rewrite the policy that governs it. An operator has to name someone deliberately.

    Synthesized with the variable explicitly absent rather than merely unset in this
    shell. The first version of this test read whatever the developer's environment
    happened to hold and failed the moment `.env` was sourced.
    """
    template = _synth(enable_cloudfront=False)

    functions = _resources_of_type(template, "AWS::Lambda::Function")
    env = functions[0]["Properties"]["Environment"]["Variables"]

    assert env["GUARDRAIL_POLICY_ADMIN_KEY_IDS"] == ""
    assert env["GUARDRAIL_POLICY_REFRESH_SECONDS"] == "30"


def test_a_configured_policy_admin_reaches_the_deployment() -> None:
    """The other half of the contract.

    Asserting only the empty default would pass just as well if the variable were dropped
    on the floor entirely -- and policy administration would then be impossible to enable,
    which is a different failure but still a failure.
    """
    template = _synth(
        enable_cloudfront=False,
        overrides={
            "GUARDRAIL_POLICY_ADMIN_KEY_IDS": "acme-policy-admin,acme-break-glass",
            "GUARDRAIL_POLICY_REFRESH_SECONDS": "5",
        },
    )

    functions = _resources_of_type(template, "AWS::Lambda::Function")
    env = functions[0]["Properties"]["Environment"]["Variables"]

    assert env["GUARDRAIL_POLICY_ADMIN_KEY_IDS"] == "acme-policy-admin,acme-break-glass"
    assert env["GUARDRAIL_POLICY_REFRESH_SECONDS"] == "5"


def test_policy_versioning_added_no_capacity() -> None:
    """Published bundles share the audit table on purpose.

    The account's whole free allowance is 25 WCU / 25 RCU and 15/15 is already committed.
    A separate policies table -- or one more index -- would have to come out of the
    remaining 10, so this asserts the design decision rather than trusting it.
    """
    template = _synth(enable_cloudfront=False)

    tables = _resources_of_type(template, "AWS::DynamoDB::Table")
    assert len(tables) == 1, "policy versioning must not introduce a second table"

    write = read = 0
    for table in tables:
        throughput = table["Properties"]["ProvisionedThroughput"]
        write += throughput["WriteCapacityUnits"]
        read += throughput["ReadCapacityUnits"]
        indexes = table["Properties"].get("GlobalSecondaryIndexes", [])
        assert len(indexes) == 2, "a third index would push a single write past the free tier"
        for index in indexes:
            write += index["ProvisionedThroughput"]["WriteCapacityUnits"]
            read += index["ProvisionedThroughput"]["ReadCapacityUnits"]

    assert (write, read) == (15, 15), f"capacity drifted to {write} WCU / {read} RCU of 25"


def test_the_alarm_budget_stays_inside_the_free_tier() -> None:
    """CloudWatch gives 10 standard-resolution alarms free, and 3 dashboards.

    Asserted for the same reason the metric budget is: adding "just one more alarm" is
    a one-line change that looks harmless in review and is discovered on an invoice.
    Headroom is left deliberately, so the next genuine need during an incident does not
    have to displace something.
    """
    template = _synth(enable_cloudfront=False)

    alarms = _resources_of_type(template, "AWS::CloudWatch::Alarm")
    dashboards = _resources_of_type(template, "AWS::CloudWatch::Dashboard")

    assert len(alarms) <= 10, f"{len(alarms)} alarms, over the free 10"
    assert len(dashboards) <= 3, f"{len(dashboards)} dashboards, over the free 3"
    assert alarms, "no alarms configured; the deployment would be unmonitored"


def test_every_alarm_notifies_somewhere() -> None:
    """An alarm with no action is a red square on a page nobody opens."""
    template = _synth(enable_cloudfront=False)

    for alarm in _resources_of_type(template, "AWS::CloudWatch::Alarm"):
        name = alarm["Properties"].get("AlarmName")
        assert alarm["Properties"].get("AlarmActions"), f"{name} has no alarm action"


def test_alarms_do_not_fire_on_an_idle_service() -> None:
    """This service is idle much of the time. An alarm that fires because nothing
    happened is one people learn to ignore, which costs more than it was ever worth."""
    template = _synth(enable_cloudfront=False)

    for alarm in _resources_of_type(template, "AWS::CloudWatch::Alarm"):
        name = alarm["Properties"].get("AlarmName")
        assert alarm["Properties"].get("TreatMissingData") == "notBreaching", (
            f"{name} would alarm on missing data"
        )


def test_provisioned_concurrency_is_never_used() -> None:
    """*Provisioned* concurrency bills by the hour and must never appear. Reserved
    concurrency is the free one, handled separately below."""
    template = _synth(enable_cloudfront=False)

    assert not _resources_of_type(template, "AWS::Lambda::ProvisionedConcurrencyConfig"), (
        "provisioned concurrency costs money"
    )


def test_reserved_concurrency_is_unset_by_default() -> None:
    """It has to be, on this account.

    A new AWS account has a total ConcurrentExecutions quota of 10, and AWS rejects any
    reservation that would leave fewer than 10 unreserved -- so the maximum reservable
    value here is exactly zero. The first deploy attempt failed on precisely that. The
    ceiling still exists; the account quota enforces it instead.
    """
    template = _synth(enable_cloudfront=False)

    function = _resources_of_type(template, "AWS::Lambda::Function")[0]
    assert "ReservedConcurrentExecutions" not in function["Properties"]


def test_reserved_concurrency_is_applied_once_the_quota_allows_it() -> None:
    """The other half of the contract. Asserting only the default would pass just as well
    if the setting had been dropped entirely, leaving it impossible to enable when the
    account quota is eventually raised."""
    template = _synth(enable_cloudfront=False, overrides={"GUARDRAIL_RESERVED_CONCURRENCY": "50"})

    function = _resources_of_type(template, "AWS::Lambda::Function")[0]
    assert function["Properties"].get("ReservedConcurrentExecutions") == 50
