"""Infrastructure assertions on the console stack.

The console is the one deliberately *public* thing in this project, which makes it worth
testing carefully rather than less: a bucket that is public because somebody meant it to
be is fine, and a bucket that is public because a default drifted is an incident.

Three properties are load-bearing:

1. **Only the bucket policy makes anything public.** Public ACLs stay blocked, so an
   object uploaded with the wrong ACL cannot widen access beyond what is declared here.
2. **The bucket name is deterministic.** The service stack's CORS allowlist has to name
   this origin, and an allowlist that can only be written after the bucket exists would
   make a first deploy a two-pass affair.
3. **`index.html` is not cached immutably.** It is the only unfingerprinted file; caching
   it hard would leave browsers serving the previous build after every deploy, which
   presents as "the fix did not deploy".
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

INFRA_DIR = Path(__file__).resolve().parents[2] / "infra"
sys.path.insert(0, str(INFRA_DIR))


def _fake_build(root: Path) -> Path:
    """Write the smallest thing the stack will accept as a built console.

    The alternative -- pointing at the real `apps/console-ui/dist` -- makes the result
    depend on whether somebody has run `npm run build` on this machine. It passed locally
    and failed in CI, where the test job does not build the frontend. A test whose result
    moves with ambient state cannot be trusted in either direction: it can mask a
    regression as easily as invent one.
    """
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<!doctype html><title>t</title>", encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text("// bundle", encoding="utf-8")
    (dist / "assets" / "index-abc123.css").write_text("/* styles */", encoding="utf-8")
    return dist


def _synth(*, skip_bundling: bool = True, dist: Path | None = None) -> dict[str, Any]:
    """Synthesize ConsoleStack and return its CloudFormation template.

    With `skip_bundling=False` the stack really uploads an asset, so `dist` must point at
    a directory that looks like a build. Callers pass a `tmp_path`-backed one.
    """
    import aws_cdk as cdk
    from aws_cdk import assertions

    os.environ["GUARDRAIL_SKIP_BUNDLING"] = "1" if skip_bundling else "0"

    import stacks.console_stack as console_stack

    importlib.reload(console_stack)

    if dist is not None:
        console_stack.CONSOLE_DIST = dist

    app = cdk.App()
    stack = console_stack.ConsoleStack(
        app,
        "TestConsole",
        stage="dev",
        env=cdk.Environment(account="111122223333", region="us-east-1"),
    )
    template: dict[str, Any] = assertions.Template.from_stack(stack).to_json()
    return template


@pytest.fixture(autouse=True)
def _restore_env() -> Iterator[None]:
    saved = os.environ.get("GUARDRAIL_SKIP_BUNDLING")
    yield
    if saved is None:
        os.environ.pop("GUARDRAIL_SKIP_BUNDLING", None)
    else:
        os.environ["GUARDRAIL_SKIP_BUNDLING"] = saved


def _resources_of_type(template: dict[str, Any], type_name: str) -> list[dict[str, Any]]:
    return [
        resource
        for resource in template.get("Resources", {}).values()
        if resource.get("Type") == type_name
    ]


# ---------------------------------------------------------------------------
# Public, but only on purpose
# ---------------------------------------------------------------------------


def test_public_acls_stay_blocked_so_only_the_policy_grants_access() -> None:
    """The bucket is world-readable by design. That must come from exactly one place.

    With `BlockPublicAcls` off, an object written with `public-read` would be reachable
    even if the bucket policy were later tightened -- two independent grant paths, only
    one of which anybody reviews.
    """
    template = _synth()

    buckets = _resources_of_type(template, "AWS::S3::Bucket")
    assert len(buckets) == 1

    config = buckets[0]["Properties"]["PublicAccessBlockConfiguration"]
    assert config["BlockPublicAcls"] is True
    assert config["IgnorePublicAcls"] is True
    # These two are off deliberately: they are what permits the read policy below.
    assert config["BlockPublicPolicy"] is False
    assert config["RestrictPublicBuckets"] is False


def test_the_public_policy_grants_read_only() -> None:
    """A write grant here would let anyone replace the console with their own page --
    and the page takes an API key."""
    template = _synth()

    policies = _resources_of_type(template, "AWS::S3::BucketPolicy")
    statements = [
        statement
        for policy in policies
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if statement.get("Principal") in ("*", {"AWS": "*"})
    ]
    assert statements, "no public statement found; the console would not be reachable"

    for statement in statements:
        actions = statement["Action"]
        actions = actions if isinstance(actions, list) else [actions]
        assert all(action.startswith("s3:Get") or action == "s3:List*" for action in actions), (
            f"public statement grants more than read: {actions}"
        )


def test_the_bucket_name_is_deterministic() -> None:
    """So GUARDRAIL_CONSOLE_ORIGINS can name this origin before it exists."""
    template = _synth()

    name = _resources_of_type(template, "AWS::S3::Bucket")[0]["Properties"]["BucketName"]

    assert name == "guardrail-console-dev-111122223333"


def test_the_origin_this_page_sends_is_published_as_an_output() -> None:
    """CORS matches the Origin header verbatim, scheme included. Making an operator
    reconstruct that string by hand is how an allowlist ends up subtly wrong -- and the
    symptom is every console request failing with no server-side log at all."""
    template = _synth()

    outputs = template.get("Outputs", {})
    assert "ConsoleAllowedOrigin" in outputs

    value = json.dumps(outputs["ConsoleAllowedOrigin"]["Value"])
    assert "https://" in value


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_index_html_is_never_cached_immutably(tmp_path: Path) -> None:
    """Vite fingerprints its assets; index.html is the one file it does not. Serving it
    with a year-long immutable cache would keep returning the previous build to anyone
    who has visited before -- which reads as "the deploy did not work"."""
    template = _synth(skip_bundling=False, dist=_fake_build(tmp_path))

    deployments = _resources_of_type(template, "Custom::CDKBucketDeployment")
    assert len(deployments) == 2, "expected separate asset and index deployments"

    immutable = [
        deployment
        for deployment in deployments
        if any("immutable" in str(value) for value in deployment["Properties"].values())
    ]
    assert len(immutable) == 1

    excluded = immutable[0]["Properties"].get("Exclude", [])
    assert "index.html" in excluded, (
        "the immutable cache deployment must exclude index.html, or every deploy "
        f"would be invisible to returning browsers. Exclude was {excluded}"
    )


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def test_no_paid_resources() -> None:
    """Reuses the CI cost gate, so the console is held to the same standard as the
    service. In particular: no CloudFront distribution appears by accident."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    from check_banned_resources import BANNED_TYPES

    template = _synth()

    found = {resource.get("Type") for resource in template.get("Resources", {}).values()}
    assert not (found & BANNED_TYPES.keys())


def test_the_bucket_is_disposable() -> None:
    """It holds a build artifact and nothing else. Retaining it on stack deletion would
    leave an orphaned public bucket behind -- the opposite of the audit table, which is
    retained precisely because it is evidence."""
    template = _synth()

    bucket = next(
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::S3::Bucket"
    )

    assert bucket["DeletionPolicy"] == "Delete"


def test_old_builds_do_not_accumulate_forever() -> None:
    """Each deploy writes a new set of fingerprinted assets. Without expiry the bucket
    creeps toward the 5 GB free allowance one build at a time."""
    template = _synth()

    bucket = next(
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::S3::Bucket"
    )

    rules = bucket["Properties"]["LifecycleConfiguration"]["Rules"]
    assert any(rule.get("NoncurrentVersionExpiration") for rule in rules)


# ---------------------------------------------------------------------------
# The build has to exist
# ---------------------------------------------------------------------------


def test_synth_fails_loudly_when_the_console_has_not_been_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deploying an unbuilt console publishes an empty bucket, and a console returning
    404 reads as a *service* outage -- somebody would start debugging the Lambda."""
    import aws_cdk as cdk
    import stacks.console_stack as console_stack

    os.environ["GUARDRAIL_SKIP_BUNDLING"] = "0"
    importlib.reload(console_stack)
    monkeypatch.setattr(console_stack, "CONSOLE_DIST", Path("/nonexistent/console/dist"))

    app = cdk.App()
    with pytest.raises(ValueError, match="has not been built"):
        console_stack.ConsoleStack(
            app,
            "Unbuilt",
            stage="dev",
            env=cdk.Environment(account="111122223333", region="us-east-1"),
        )
