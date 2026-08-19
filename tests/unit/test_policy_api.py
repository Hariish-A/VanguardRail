"""The policy lifecycle and simulation endpoints, over HTTP.

Two of these matter more than the rest:

* `test_an_agent_key_cannot_publish_policy` -- an agent able to rewrite the policy that
  governs it is not governed. This is the privilege boundary the whole product rests on.
* `test_activating_a_new_version_changes_live_behaviour_without_a_redeploy` -- the actual
  M4 deliverable, end to end: publish, activate, and watch `/v1/evaluate` change its mind.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from guardrail_service import auth, dependencies

AGENT_KEY = "agent-key-value"
ADMIN_KEY = "admin-key-value"
TENANT = "acme"


@pytest.fixture(autouse=True)
def _wire_test_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Two keys in the same tenant: one that may only evaluate, one that may also
    publish. The distinction is the point of most of this file."""
    key_table = {
        auth.hash_key(AGENT_KEY): {
            "key_id": "agent-1",
            "tenant_id": TENANT,
            "name": "support bot",
        },
        auth.hash_key(ADMIN_KEY): {
            "key_id": "admin-1",
            "tenant_id": TENANT,
            "name": "platform team",
        },
    }
    monkeypatch.setenv("GUARDRAIL_API_KEYS_JSON", json.dumps(key_table))
    monkeypatch.setenv(auth.POLICY_ADMIN_ENV, "admin-1")
    monkeypatch.setenv("GUARDRAIL_STAGE", "local")
    # A zero refresh window makes activation visible on the very next request, so these
    # tests exercise the reload path rather than the cache's staleness bound.
    monkeypatch.setenv("GUARDRAIL_POLICY_REFRESH_SECONDS", "0")

    auth.reset_key_cache()
    from guardrail_service.config import get_settings

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


def _admin(**extra: str) -> dict[str, str]:
    return {"x-api-key": ADMIN_KEY, **extra}


def _agent(**extra: str) -> dict[str, str]:
    return {"x-api-key": AGENT_KEY, **extra}


def _bundle(threshold: int, *, mode: str = "enforce") -> dict[str, Any]:
    return {
        "apiVersion": "guardrail/v1",
        "metadata": {"bundle_id": "default", "version": 1, "mode": mode},
        "defaults": {"effect": "allow", "resolution": "most_restrictive"},
        "rules": [
            {
                "id": "db-bulk-delete",
                "severity": "critical",
                "match": {
                    "tool": "db.delete_records",
                    "all": [{"path": "derived.record_count", "op": "gt", "value": threshold}],
                },
                "effect": "block",
                "message": "Blocked: {derived.record_count} records exceeds the limit.",
            }
        ],
    }


def _evaluate(client: TestClient, count: int) -> Any:
    return client.post(
        "/v1/evaluate",
        json={
            "agent_id": "bot",
            "session_id": "s1",
            "tool": "db.delete_records",
            "arguments": {"count": count},
        },
        headers=_agent(),
    )


# ---------------------------------------------------------------------------
# Authorization -- the privilege boundary
# ---------------------------------------------------------------------------


def test_an_agent_key_cannot_publish_policy(client: TestClient) -> None:
    """**The most important test in this file.**

    An agent that can publish its own policy can approve its own next action. Holding a
    valid API key must not imply the right to change the rules.
    """
    response = client.post("/v1/policies", json={"bundle": _bundle(1)}, headers=_agent())

    assert response.status_code == 403
    assert "may not change policy" in response.json()["detail"]


def test_an_agent_key_cannot_activate_a_version(client: TestClient) -> None:
    client.post("/v1/policies", json={"bundle": _bundle(100)}, headers=_admin())

    response = client.post("/v1/policies/versions/1/activate", headers=_agent())

    assert response.status_code == 403


def test_nobody_can_publish_when_no_administrator_is_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing closed by default is deliberate. The alternative default -- any
    authenticated caller may rewrite policy -- is insecure the moment one agent key
    leaks, and insecure silently."""
    monkeypatch.delenv(auth.POLICY_ADMIN_ENV, raising=False)

    response = client.post("/v1/policies", json={"bundle": _bundle(1)}, headers=_admin())

    assert response.status_code == 403
    assert auth.POLICY_ADMIN_ENV in response.json()["detail"]


def test_an_agent_key_may_still_read_policy(client: TestClient) -> None:
    """An agent knowing the rules it is bound by is not a risk; editing them is."""
    assert client.get("/v1/policies", headers=_agent()).status_code == 200
    assert client.get("/v1/policies/active", headers=_agent()).status_code == 200


def test_policy_endpoints_reject_unauthenticated_callers(client: TestClient) -> None:
    assert client.get("/v1/policies").status_code == 401
    assert client.post("/v1/policies", json={"bundle": _bundle(1)}).status_code == 401
    assert client.post("/v1/simulate", json={"action": {}}).status_code == 401


# ---------------------------------------------------------------------------
# Publish / activate / rollback
# ---------------------------------------------------------------------------


def test_publishing_does_not_change_behaviour(client: TestClient) -> None:
    """Publishing is safe enough to run in CI on every merge; activating is the
    deliberate act."""
    assert _evaluate(client, 500).json()["decision"] == "block"

    response = client.post(
        "/v1/policies",
        json={"bundle": _bundle(10_000), "description": "much looser"},
        headers=_admin(),
    )

    assert response.status_code == 201
    assert response.json()["activated"] is False
    assert _evaluate(client, 500).json()["decision"] == "block", (
        "a published-but-not-activated bundle must not affect decisions"
    )


def test_activating_a_new_version_changes_live_behaviour_without_a_redeploy(
    client: TestClient,
) -> None:
    """The M4 deliverable, end to end."""
    assert _evaluate(client, 500).json()["decision"] == "block"

    client.post("/v1/policies", json={"bundle": _bundle(10_000)}, headers=_admin())
    activation = client.post("/v1/policies/versions/1/activate", headers=_admin())

    assert activation.status_code == 200
    assert activation.json()["active_version"] == 1

    after = _evaluate(client, 500).json()
    assert after["decision"] == "allow"
    assert after["bundle_version"] == 1


def test_rollback_restores_the_previous_behaviour(client: TestClient) -> None:
    """Rollback is `activate` with a lower number -- no separate code path, so no
    separate rollback bug."""
    client.post("/v1/policies", json={"bundle": _bundle(100)}, headers=_admin())
    client.post("/v1/policies", json={"bundle": _bundle(10_000)}, headers=_admin())

    client.post("/v1/policies/versions/2/activate", headers=_admin())
    assert _evaluate(client, 500).json()["decision"] == "allow"

    rollback = client.post("/v1/policies/versions/1/activate", headers=_admin())

    assert rollback.json()["direction"] == "rollback"
    assert _evaluate(client, 500).json()["decision"] == "block"


def test_the_activation_direction_is_named_explicitly(client: TestClient) -> None:
    """A rollback during an incident should be identifiable in the log without
    arithmetic."""
    client.post("/v1/policies", json={"bundle": _bundle(100)}, headers=_admin())
    client.post("/v1/policies", json={"bundle": _bundle(200)}, headers=_admin())

    client.post("/v1/policies/versions/1/activate", headers=_admin())
    forward = client.post("/v1/policies/versions/2/activate", headers=_admin())
    back = client.post("/v1/policies/versions/1/activate", headers=_admin())

    assert forward.json()["direction"] == "rollforward"
    assert back.json()["direction"] == "rollback"
    assert back.json()["previous_version"] == 2


def test_publish_and_activate_in_one_call(client: TestClient) -> None:
    response = client.post(
        "/v1/policies?activate=true", json={"bundle": _bundle(10_000)}, headers=_admin()
    )

    assert response.status_code == 201
    assert response.json()["activated"] is True
    assert _evaluate(client, 500).json()["decision"] == "allow"


def test_activating_a_version_that_does_not_exist_is_a_404(client: TestClient) -> None:
    assert client.post("/v1/policies/versions/99/activate", headers=_admin()).status_code == 404


def test_publishing_accepts_yaml(client: TestClient) -> None:
    """YAML is what people author and review in pull requests. Forcing a conversion step
    adds a place for a mistake to be introduced."""
    import yaml

    response = client.post(
        "/v1/policies",
        json={"yaml": yaml.safe_dump(_bundle(10_000)), "description": "from yaml"},
        headers=_admin(),
    )

    assert response.status_code == 201
    assert response.json()["version"] == 1


def test_publishing_rejects_both_formats_at_once(client: TestClient) -> None:
    response = client.post(
        "/v1/policies",
        json={"bundle": _bundle(1), "yaml": "apiVersion: guardrail/v1"},
        headers=_admin(),
    )

    assert response.status_code == 422


def test_an_unparseable_bundle_is_rejected_before_storage(client: TestClient) -> None:
    broken = _bundle(100)
    broken["rules"][0]["match"]["all"][0]["op"] = "greater-than"

    response = client.post("/v1/policies", json={"bundle": broken}, headers=_admin())

    assert response.status_code == 422
    assert client.get("/v1/policies", headers=_admin()).json()["versions"] == []


def test_versions_are_listed_newest_first_with_the_active_one_marked(
    client: TestClient,
) -> None:
    client.post("/v1/policies", json={"bundle": _bundle(100)}, headers=_admin())
    client.post("/v1/policies", json={"bundle": _bundle(200)}, headers=_admin())
    client.post("/v1/policies/versions/1/activate", headers=_admin())

    body = client.get("/v1/policies", headers=_admin()).json()

    assert [v["version"] for v in body["versions"]] == [2, 1]
    assert body["active_version"] == 1
    assert [v["is_active"] for v in body["versions"]] == [False, True]


def test_who_published_and_who_activated_is_recorded(client: TestClient) -> None:
    """Attribution is the point of a policy audit trail."""
    client.post("/v1/policies", json={"bundle": _bundle(100)}, headers=_admin())
    activation = client.post("/v1/policies/versions/1/activate", headers=_admin())

    listing = client.get("/v1/policies", headers=_admin()).json()

    assert "admin-1" in listing["versions"][0]["published_by"]
    assert "admin-1" in activation.json()["activated_by"]


def test_the_active_endpoint_reports_where_the_bundle_came_from(client: TestClient) -> None:
    before = client.get("/v1/policies/active", headers=_admin()).json()
    assert before["source"] == "packaged"

    client.post("/v1/policies?activate=true", json={"bundle": _bundle(100)}, headers=_admin())

    after = client.get("/v1/policies/active", headers=_admin()).json()
    assert after["source"] == "published"
    assert after["degraded"] is False


# ---------------------------------------------------------------------------
# Validation endpoint
# ---------------------------------------------------------------------------


def test_validation_returns_200_with_valid_false_for_a_bad_bundle(
    client: TestClient,
) -> None:
    """A linting endpoint. A CI step that has to distinguish "request failed" from
    "policy is wrong" by parsing an error body is a CI step that gets it wrong."""
    broken = _bundle(100)
    broken["rules"][0]["effect"] = "obliterate"

    response = client.post("/v1/policies/validate", json={"bundle": broken}, headers=_agent())

    assert response.status_code == 200
    assert response.json()["valid"] is False


def test_validation_reports_when_a_bundle_matches_what_is_already_live(
    client: TestClient,
) -> None:
    """ "Publish v9" after an edit that changed nothing is a strong signal the file being
    edited is not the file being deployed."""
    client.post("/v1/policies?activate=true", json={"bundle": _bundle(100)}, headers=_admin())

    response = client.post(
        "/v1/policies/validate", json={"bundle": _bundle(100)}, headers=_agent()
    ).json()

    assert response["valid"] is True
    assert response["matches_active"] is True


def test_validation_stores_nothing(client: TestClient) -> None:
    client.post("/v1/policies/validate", json={"bundle": _bundle(100)}, headers=_admin())

    assert client.get("/v1/policies", headers=_admin()).json()["versions"] == []


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------


def _simulate(client: TestClient, count: int, **extra: Any) -> Any:
    return client.post(
        "/v1/simulate",
        json={
            "action": {
                "agent_id": "bot",
                "session_id": "s1",
                "tool": "db.delete_records",
                "arguments": {"count": count},
            },
            **extra,
        },
        headers=_agent(),
    )


def test_simulate_reports_the_active_policys_verdict(client: TestClient) -> None:
    body = _simulate(client, 500).json()

    assert body["decision"] == "block"
    assert body["bundle_source"] == "active"
    assert body["simulated"] is True
    assert body["dry_run"] is True


def test_simulate_writes_no_audit_record(client: TestClient) -> None:
    """A simulation is not an attempt. Recording thousands of what-ifs alongside real
    decisions would dilute the one log meant to answer "what did this agent do"."""
    for count in (5, 500, 5000):
        _simulate(client, count)

    audit = client.get("/v1/audit", headers=_agent()).json()

    assert audit["count"] == 0


def test_simulate_can_evaluate_an_unpublished_candidate_bundle(client: TestClient) -> None:
    """Reviewing a pull request should not require publishing the branch."""
    body = _simulate(client, 500, bundle=_bundle(10_000)).json()

    assert body["decision"] == "allow"
    assert body["bundle_source"] == "candidate"


def test_simulate_can_evaluate_a_published_but_inactive_version(
    client: TestClient,
) -> None:
    client.post("/v1/policies", json={"bundle": _bundle(10_000)}, headers=_admin())

    body = _simulate(client, 500, version=1).json()

    assert body["decision"] == "allow"
    assert body["bundle_source"] == "version"
    assert _evaluate(client, 500).json()["decision"] == "block", (
        "simulating against a version must not activate it"
    )


def test_simulate_rejects_a_candidate_and_a_version_together(client: TestClient) -> None:
    assert _simulate(client, 5, bundle=_bundle(1), version=1).status_code == 422


def test_simulate_rejects_an_invalid_candidate_bundle(client: TestClient) -> None:
    broken = _bundle(100)
    broken["rules"][0]["match"]["all"][0]["path"] = "derived.nonsense"

    assert _simulate(client, 5, bundle=broken).status_code == 422


def test_simulate_returns_the_derived_facts(client: TestClient) -> None:
    """The most useful field when a rule did not fire and the author cannot see why."""
    body = _simulate(client, 500).json()

    assert body["derived"]["record_count"] == 500


def test_simulate_preserves_unknown_as_a_distinct_value(client: TestClient) -> None:
    """Collapsing UNKNOWN into null would hide the single most common reason a rule
    fired unexpectedly."""
    body = client.post(
        "/v1/simulate",
        json={
            "action": {
                "agent_id": "bot",
                "session_id": "s1",
                "tool": "db.delete_records",
                "arguments": {"where": "1=1"},
            }
        },
        headers=_agent(),
    ).json()

    assert body["derived"]["record_count"] == "UNKNOWN"
    assert "derived.record_count" in body["unknown_paths"]
    assert body["decision"] == "block"


def test_simulate_uses_the_tenant_from_the_key_not_the_body(client: TestClient) -> None:
    """Otherwise a caller could read another tenant's policy back rule by rule."""
    client.post("/v1/policies?activate=true", json={"bundle": _bundle(10_000)}, headers=_admin())

    body = client.post(
        "/v1/simulate",
        json={
            "action": {
                "agent_id": "bot",
                "session_id": "s1",
                "tool": "db.delete_records",
                "arguments": {"count": 500},
                "tenant_id": "someone-else",
            }
        },
        headers=_agent(),
    ).json()

    assert body["decision"] == "allow", "must resolve this tenant's active policy"


def test_a_shadow_bundle_downgrades_a_block_in_simulation(client: TestClient) -> None:
    """A simulation that ignored shadow mode would report `block` for a bundle whose
    whole purpose is to not block yet."""
    body = _simulate(client, 500, bundle=_bundle(100, mode="shadow")).json()

    assert body["decision"] == "log_and_allow"
    assert body["allowed"] is True
    assert [r["rule_id"] for r in body["matched_rules"]] == ["db-bulk-delete"]


def test_a_shadow_bundle_does_not_restrain_the_enforcement_path(client: TestClient) -> None:
    """Bundle-wide dry run: a policy can be trialled against live traffic before it is
    enforced."""
    client.post(
        "/v1/policies?activate=true",
        json={"bundle": _bundle(100, mode="shadow")},
        headers=_admin(),
    )

    body = _evaluate(client, 500).json()

    assert body["decision"] == "log_and_allow"
    assert body["allowed"] is True
    assert [r["rule_id"] for r in body["matched_rules"]] == ["db-bulk-delete"], (
        "the rule must still be recorded as matched, or shadow mode teaches nothing"
    )
