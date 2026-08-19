"""Roles: holding a key must not imply the right to approve.

The load-bearing test here is `test_an_agent_cannot_approve_the_action_its_own_policy_held`.

Before roles existed, `/v1/decisions/{id}/resolve` required only a valid API key. That made
`require_hitl` -- the control whose entire purpose is "pause for a *human*" -- resolvable by
the agent being paused. It was verified against the live deployment: the AWS-hosted agent
had an external email held, then approved it with its own key, and the audit chain recorded
`reviewer: the-agent-itself`.

The identical reasoning had already been written down for policy administration:

    "An agent whose key can rewrite the policy governing it is not governed."

It simply was never extended to approval.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from guardrail_service import auth, dependencies
from guardrail_service.auth import ADMIN, AGENT, REVIEWER, AuthenticatedCaller, role_rank

AGENT_KEY = "agent-key"
REVIEWER_KEY = "reviewer-key"
ADMIN_KEY = "admin-key"
LEGACY_KEY = "legacy-key"


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
        # No `role` at all -- exactly what every currently deployed key looks like.
        auth.hash_key(LEGACY_KEY): {
            "key_id": "legacy-1",
            "tenant_id": "acme",
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


def _hold(client: TestClient, key: str = AGENT_KEY) -> str:
    """Trigger an action policy holds for human review, and return its decision id."""
    response = client.post(
        "/v1/evaluate",
        json={
            "agent_id": "bot",
            "session_id": "s1",
            "tool": "email.send",
            "arguments": {"to": ["auditor@external-firm.com"], "subject": "Q3"},
        },
        headers={"x-api-key": key},
    )
    body = response.json()
    assert body["decision"] == "require_hitl", body
    return str(body["hitl"]["decision_id"])


def _resolve(client: TestClient, decision_id: str, key: str, approve: bool = True) -> Any:
    return client.post(
        f"/v1/decisions/{decision_id}/resolve",
        json={"approve": approve, "reviewer": "whoever", "reason": "because"},
        headers={"x-api-key": key},
    )


# ---------------------------------------------------------------------------
# The hole this closes
# ---------------------------------------------------------------------------


def test_an_agent_cannot_approve_the_action_its_own_policy_held(client: TestClient) -> None:
    """**The regression test for a verified live defect.**

    `require_hitl` means "pause for a human". If the agent can resolve it, the control is
    circumvented by exactly the party it exists to restrain.
    """
    decision_id = _hold(client, AGENT_KEY)

    response = _resolve(client, decision_id, AGENT_KEY, approve=True)

    assert response.status_code == 403, response.text
    assert "may not approve" in response.json()["detail"]

    still_held = client.get(f"/v1/decisions/{decision_id}", headers={"x-api-key": AGENT_KEY}).json()
    assert still_held["status"] == "pending"
    assert still_held["allows_execution"] is False, "the action must remain refused"


def test_a_reviewer_can_approve(client: TestClient) -> None:
    """The control has to still work, or the fix is just an outage."""
    decision_id = _hold(client)

    assert _resolve(client, decision_id, REVIEWER_KEY).status_code == 200

    resolved = client.get(
        f"/v1/decisions/{decision_id}", headers={"x-api-key": REVIEWER_KEY}
    ).json()
    assert resolved["status"] == "approved"
    assert resolved["allows_execution"] is True


def test_an_admin_can_also_approve(client: TestClient) -> None:
    """Roles are ordered: admin includes reviewer."""
    decision_id = _hold(client)

    assert _resolve(client, decision_id, ADMIN_KEY).status_code == 200


def test_an_agent_cannot_deny_either(client: TestClient) -> None:
    """Denying is also a review decision. An agent that could deny could suppress an
    action a human would have approved."""
    decision_id = _hold(client)

    assert _resolve(client, decision_id, AGENT_KEY, approve=False).status_code == 403


# ---------------------------------------------------------------------------
# Least privilege by default
# ---------------------------------------------------------------------------


def test_a_key_with_no_role_defaults_to_agent(client: TestClient) -> None:
    """Every currently deployed key looks like this. The default must restrict, not
    grant -- a key table that forgets a role must not hand out approval rights."""
    decision_id = _hold(client, LEGACY_KEY)

    assert _resolve(client, decision_id, LEGACY_KEY).status_code == 403


def test_an_unrecognised_role_is_treated_as_agent() -> None:
    """A typo in a role must restrict rather than escalate."""
    assert role_rank("revieweer") == role_rank(AGENT)
    assert role_rank("superuser") == role_rank(AGENT)
    assert role_rank("") == role_rank(AGENT)


def test_roles_are_ordered_so_higher_includes_lower() -> None:
    agent = AuthenticatedCaller("k", "t", "n", AGENT)
    reviewer = AuthenticatedCaller("k", "t", "n", REVIEWER)
    admin = AuthenticatedCaller("k", "t", "n", ADMIN)

    assert not agent.can(REVIEWER) and not agent.can(ADMIN)
    assert reviewer.can(AGENT) and reviewer.can(REVIEWER) and not reviewer.can(ADMIN)
    assert admin.can(AGENT) and admin.can(REVIEWER) and admin.can(ADMIN)


# ---------------------------------------------------------------------------
# What an agent must still be able to do
# ---------------------------------------------------------------------------


def test_an_agent_can_still_do_its_job(client: TestClient) -> None:
    """The fix must not break the hot path. An agent still evaluates, simulates, and reads
    the log -- it simply cannot approve."""
    headers = {"x-api-key": AGENT_KEY}

    evaluate = client.post(
        "/v1/evaluate",
        json={
            "agent_id": "a",
            "session_id": "s",
            "tool": "file.read",
            "arguments": {"path": "/tmp/x"},
        },
        headers=headers,
    )
    assert evaluate.status_code == 200

    assert client.get("/v1/audit", headers=headers).status_code == 200
    assert client.get("/v1/audit/verify", headers=headers).status_code == 200
    assert client.get("/v1/decisions", headers=headers).status_code == 200
    assert client.get("/v1/policies", headers=headers).status_code == 200
    assert (
        client.post(
            "/v1/simulate",
            json={
                "action": {
                    "agent_id": "a",
                    "session_id": "s",
                    "tool": "file.read",
                    "arguments": {"path": "/tmp/x"},
                }
            },
            headers=headers,
        ).status_code
        == 200
    )


def test_reading_the_queue_is_not_the_same_as_resolving_it(client: TestClient) -> None:
    """An agent seeing that its action is pending is useful and harmless -- that is how it
    reports status. Acting on it is the part that must be refused."""
    decision_id = _hold(client, AGENT_KEY)
    headers = {"x-api-key": AGENT_KEY}

    assert client.get(f"/v1/decisions/{decision_id}", headers=headers).status_code == 200
    assert _resolve(client, decision_id, AGENT_KEY).status_code == 403


# ---------------------------------------------------------------------------
# Policy administration, now expressed as a role
# ---------------------------------------------------------------------------


def _publish(client: TestClient, key: str) -> Any:
    return client.post(
        "/v1/policies",
        json={
            "bundle": {
                "apiVersion": "guardrail/v1",
                "metadata": {"bundle_id": "default"},
                "rules": [],
            }
        },
        headers={"x-api-key": key},
    )


def test_the_admin_role_grants_policy_administration(client: TestClient) -> None:
    """Previously this required naming a key id in an environment variable. The role is now
    the intended mechanism, so a key table alone is enough."""
    assert _publish(client, ADMIN_KEY).status_code == 201


def test_a_reviewer_cannot_change_policy(client: TestClient) -> None:
    """Approving today's actions and rewriting the rules are different privileges.
    Collapsing them would force every reviewer to hold the highest one."""
    assert _publish(client, REVIEWER_KEY).status_code == 403


def test_an_agent_still_cannot_change_policy(client: TestClient) -> None:
    assert _publish(client, AGENT_KEY).status_code == 403


def test_the_environment_allowlist_still_works_as_break_glass(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The env var predates roles and is kept deliberately: it can grant admin without
    reissuing a key, which matters during an incident. Existing deployments also rely on
    it, and breaking them to tidy the design would be the wrong trade."""
    monkeypatch.setenv(auth.POLICY_ADMIN_ENV, "agent-1")
    auth.reset_key_cache()

    assert _publish(client, AGENT_KEY).status_code == 201
