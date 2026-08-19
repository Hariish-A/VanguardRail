"""`GET /v1/me` — and the property that makes it worth having.

The console gates its buttons on `capabilities`. That creates a new way to be wrong: the
list can drift from what the endpoints actually enforce, and then the UI lies. It would
lie *quietly*, because a capability list is not exercised by anything else in the suite.

So the load-bearing test here is `test_capabilities_agree_with_what_the_api_enforces`,
which does not read `_capabilities` at all. It asks the server what the caller may do,
then goes and tries it, and requires the two to agree in **both** directions -- a claimed
capability that 403s, and an unclaimed one that succeeds, are both failures.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from guardrail_service import auth, dependencies

AGENT_KEY = "me-agent-key"
REVIEWER_KEY = "me-reviewer-key"
ADMIN_KEY = "me-admin-key"
LEGACY_KEY = "me-legacy-key"


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    table = {
        auth.hash_key(AGENT_KEY): {
            "key_id": "agent-1",
            "tenant_id": "acme",
            "name": "support bot",
            "role": "agent",
        },
        auth.hash_key(REVIEWER_KEY): {
            "key_id": "rev-1",
            "tenant_id": "acme",
            "name": "security oncall",
            "role": "reviewer",
        },
        auth.hash_key(ADMIN_KEY): {
            "key_id": "adm-1",
            "tenant_id": "acme",
            "name": "platform",
            "role": "admin",
        },
        auth.hash_key(LEGACY_KEY): {
            "key_id": "legacy-1",
            "tenant_id": "other",
            "name": "pre-roles key",
        },
    }
    monkeypatch.setenv("GUARDRAIL_API_KEYS_JSON", json.dumps(table))
    monkeypatch.setenv("GUARDRAIL_STAGE", "local")
    monkeypatch.delenv(auth.POLICY_ADMIN_ENV, raising=False)

    from guardrail_service.config import get_settings

    auth.reset_key_cache()
    get_settings.cache_clear()
    dependencies.reset_caches()
    yield
    auth.reset_key_cache()
    get_settings.cache_clear()
    dependencies.reset_caches()


@pytest.fixture
def client() -> TestClient:
    from guardrail_service.app import app

    return TestClient(app)


def _me(client: TestClient, key: str) -> Any:
    return client.get("/v1/me", headers={"x-api-key": key}).json()


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_it_reports_the_callers_identity(client: TestClient) -> None:
    body = _me(client, REVIEWER_KEY)

    assert body["key_id"] == "rev-1"
    assert body["tenant_id"] == "acme"
    assert body["name"] == "security oncall"
    assert body["role"] == "reviewer"


def test_it_requires_a_key(client: TestClient) -> None:
    """It doubles as the console's credential check, so it must actually refuse."""
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me", headers={"x-api-key": "wrong"}).status_code == 401


def test_it_never_echoes_the_credential(client: TestClient) -> None:
    """The key arrives in a header; returning it, or its hash, would only create a
    second place for it to leak -- into a console's network tab, or a screenshot."""
    raw = client.get("/v1/me", headers={"x-api-key": REVIEWER_KEY}).text

    assert REVIEWER_KEY not in raw
    assert auth.hash_key(REVIEWER_KEY) not in raw


def test_a_key_with_no_role_reports_agent(client: TestClient) -> None:
    """The default must show as the least privilege, not as an absence."""
    body = _me(client, LEGACY_KEY)

    assert body["role"] == "agent"
    assert "resolve_decisions" not in body["capabilities"]


def test_the_tenant_comes_from_the_key(client: TestClient) -> None:
    assert _me(client, LEGACY_KEY)["tenant_id"] == "other"
    assert _me(client, AGENT_KEY)["tenant_id"] == "acme"


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


def test_an_agent_gets_no_privileged_capability(client: TestClient) -> None:
    caps = _me(client, AGENT_KEY)["capabilities"]

    assert "evaluate" in caps
    assert "read_audit" in caps
    assert "resolve_decisions" not in caps
    assert "publish_policy" not in caps


def test_roles_accumulate(client: TestClient) -> None:
    agent = set(_me(client, AGENT_KEY)["capabilities"])
    reviewer = set(_me(client, REVIEWER_KEY)["capabilities"])
    admin = set(_me(client, ADMIN_KEY)["capabilities"])

    assert agent < reviewer < admin, (agent, reviewer, admin)
    assert "resolve_decisions" in reviewer
    assert "publish_policy" in admin and "publish_policy" not in reviewer


def test_a_break_glass_admin_is_reported_as_one(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`GUARDRAIL_POLICY_ADMIN_KEY_IDS` grants publication to a key whose *role* is
    `agent`. Deriving capabilities from the role string alone would tell that operator
    they cannot do the thing they can in fact do -- during an incident, which is when
    the break-glass path is used at all."""
    monkeypatch.setenv(auth.POLICY_ADMIN_ENV, "agent-1")
    auth.reset_key_cache()

    body = _me(client, AGENT_KEY)

    assert body["role"] == "agent"
    assert "publish_policy" in body["capabilities"]


# ---------------------------------------------------------------------------
# The one that keeps the rest honest
# ---------------------------------------------------------------------------


def _attempt(client: TestClient, capability: str, key: str) -> int:
    """Actually exercise one capability and return the status code.

    Deliberately makes real requests. Comparing `_capabilities` against a hand-written
    table would only assert that the same list was typed twice.
    """
    headers = {"x-api-key": key}

    if capability == "evaluate":
        return client.post(
            "/v1/evaluate",
            json={"agent_id": "a", "session_id": "s", "tool": "file.read", "arguments": {}},
            headers=headers,
        ).status_code

    if capability == "simulate":
        return client.post(
            "/v1/simulate",
            json={
                "action": {
                    "agent_id": "a",
                    "session_id": "s",
                    "tool": "file.read",
                    "arguments": {},
                }
            },
            headers=headers,
        ).status_code

    if capability == "read_audit":
        return client.get("/v1/audit", headers=headers).status_code

    if capability == "read_decisions":
        return client.get("/v1/decisions", headers=headers).status_code

    if capability == "read_policy":
        return client.get("/v1/policies", headers=headers).status_code

    if capability == "resolve_decisions":
        held = client.post(
            "/v1/evaluate",
            json={
                "agent_id": "a",
                "session_id": "s",
                "tool": "email.send",
                "arguments": {"to": ["x@external-firm.com"]},
            },
            headers=headers,
        ).json()
        decision_id = held["hitl"]["decision_id"]
        return client.post(
            f"/v1/decisions/{decision_id}/resolve",
            json={"approve": True, "reason": "capability probe"},
            headers=headers,
        ).status_code

    if capability == "publish_policy":
        return client.post(
            "/v1/policies",
            json={
                "bundle": {
                    "apiVersion": "guardrail/v1",
                    "metadata": {"bundle_id": "default"},
                    "rules": [],
                }
            },
            headers=headers,
        ).status_code

    raise AssertionError(f"no probe written for capability {capability!r}")


ALL_CAPABILITIES = [
    "evaluate",
    "simulate",
    "read_audit",
    "read_decisions",
    "read_policy",
    "resolve_decisions",
    "publish_policy",
]


@pytest.mark.parametrize("key", [AGENT_KEY, REVIEWER_KEY, ADMIN_KEY])
def test_capabilities_agree_with_what_the_api_enforces(client: TestClient, key: str) -> None:
    """**The test that makes /v1/me trustworthy.**

    For every capability the system knows about, ask the server whether this caller has
    it, then go and try it. A claimed capability that is refused, and an unclaimed one
    that succeeds, are equally wrong: the first makes the console show a button that
    cannot work, the second makes it hide a control the operator holds.
    """
    claimed = set(_me(client, key)["capabilities"])
    assert claimed, "the caller reported no capabilities at all"

    mismatches: list[str] = []
    for capability in ALL_CAPABILITIES:
        status_code = _attempt(client, capability, key)
        permitted = status_code != 403

        if capability in claimed and not permitted:
            mismatches.append(f"{capability}: claimed, but the API returned {status_code}")
        if capability not in claimed and permitted:
            mismatches.append(f"{capability}: not claimed, but the API allowed it ({status_code})")

    assert not mismatches, (
        f"/v1/me disagrees with the endpoints for key {key!r}: {mismatches}. "
        "The console gates its UI on this list, so a drift here is a UI that lies."
    )


def test_the_capability_list_covers_every_verb_the_endpoint_can_report(
    client: TestClient,
) -> None:
    """Meta-test. If a capability were added to the router but not to ALL_CAPABILITIES,
    the agreement test above would silently stop checking it -- passing while covering
    less, which is the failure mode this repo has hit three times."""
    reported = set(_me(client, ADMIN_KEY)["capabilities"])

    assert reported <= set(ALL_CAPABILITIES), (
        f"/v1/me reports {sorted(reported - set(ALL_CAPABILITIES))}, which no probe in "
        "this file exercises. Add a probe to _attempt and list it in ALL_CAPABILITIES."
    )
