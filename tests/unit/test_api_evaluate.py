"""End-to-end API behaviour for /v1/evaluate and /v1/audit.

Runs against the real FastAPI app with an in-memory audit repository, so the request
validation, authentication, engine, chaining, and response shaping are all exercised
together -- only DynamoDB is substituted.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from guardrail_service import auth, dependencies
from guardrail_service.storage.audit import InMemoryAuditRepository

API_KEY = "test-key-do-not-use-in-production"
TENANT = "acme"


@pytest.fixture(autouse=True)
def _wire_test_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Install a known API key and a fresh in-memory audit log per test."""
    key_table = {
        auth.hash_key(API_KEY): {
            "key_id": "test-key",
            "tenant_id": TENANT,
            "name": "integration tests",
        }
    }
    monkeypatch.setenv("GUARDRAIL_API_KEYS_JSON", json.dumps(key_table))
    auth.reset_key_cache()

    repository = InMemoryAuditRepository()
    dependencies.get_audit_repository.cache_clear()
    monkeypatch.setattr(dependencies, "get_audit_repository", lambda: repository)

    # The routers imported the function directly, so patch it there too.
    from guardrail_service.routers import audit as audit_router
    from guardrail_service.routers import evaluate as evaluate_router

    monkeypatch.setattr(evaluate_router, "get_audit_repository", lambda: repository)
    monkeypatch.setattr(audit_router, "get_audit_repository", lambda: repository)

    yield

    # Only the auth cache is cleared here: monkeypatch restores
    # get_audit_repository itself, and calling cache_clear on the substituted
    # lambda would fail.
    auth.reset_key_cache()


@pytest.fixture
def client() -> TestClient:
    from guardrail_service.app import app

    return TestClient(app)


def _evaluate(client: TestClient, tool: str, arguments: dict[str, Any], **kw: Any) -> Any:
    body = {
        "agent_id": "support-bot",
        "session_id": "sess-1",
        "tool": tool,
        "arguments": arguments,
        **kw,
    }
    return client.post("/v1/evaluate", json=body, headers={"x-api-key": API_KEY})


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_evaluate_requires_an_api_key(client: TestClient) -> None:
    response = client.post("/v1/evaluate", json={"agent_id": "a", "session_id": "s", "tool": "x"})

    assert response.status_code == 401


def test_evaluate_rejects_an_unknown_api_key(client: TestClient) -> None:
    response = client.post(
        "/v1/evaluate",
        json={"agent_id": "a", "session_id": "s", "tool": "x"},
        headers={"x-api-key": "wrong-key"},
    )

    assert response.status_code == 401


def test_rejection_does_not_echo_the_submitted_key(client: TestClient) -> None:
    """Error strings reach logs, bug trackers, and screenshots."""
    secret = "super-secret-value"

    response = client.post(
        "/v1/evaluate",
        json={"agent_id": "a", "session_id": "s", "tool": "x"},
        headers={"x-api-key": secret},
    )

    assert secret not in response.text


# ---------------------------------------------------------------------------
# The problem statement's success criteria, over HTTP
# ---------------------------------------------------------------------------


def test_bulk_delete_is_blocked(client: TestClient) -> None:
    response = _evaluate(client, "db.delete_records", {"table": "users", "count": 500})

    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "block"
    assert body["allowed"] is False
    assert "db-bulk-delete" in [r["rule_id"] for r in body["matched_rules"]]
    assert "500" in body["message"]


def test_small_delete_is_allowed(client: TestClient) -> None:
    body = _evaluate(client, "db.delete_records", {"table": "users", "count": 5}).json()

    assert body["decision"] == "allow"
    assert body["allowed"] is True


def test_external_email_requires_hitl_and_returns_instructions(client: TestClient) -> None:
    body = _evaluate(client, "email.send", {"to": "partner@external.com"}).json()

    assert body["decision"] == "require_hitl"
    assert body["allowed"] is False
    assert body["hitl"] is not None
    assert body["hitl"]["decision_id"] == body["decision_id"]
    assert body["hitl"]["on_timeout"] == "deny", "silence must never become approval"
    assert body["hitl"]["timeout_seconds"] == 900


def test_internal_email_is_allowed(client: TestClient) -> None:
    body = _evaluate(client, "email.send", {"to": "bob@acme-corp.com"}).json()

    assert body["decision"] == "allow"
    assert body["hitl"] is None


def test_confidential_read_is_logged_and_allowed(client: TestClient) -> None:
    body = _evaluate(client, "file.read", {"path": "/srv/confidential/q3.pdf"}).json()

    assert body["decision"] == "log_and_allow"
    assert body["allowed"] is True, "log_and_allow must still permit execution"


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def test_every_outcome_is_audited_including_allows(client: TestClient) -> None:
    """An audit log that only records denials cannot answer 'what did this agent do'."""
    _evaluate(client, "db.delete_records", {"count": 500})
    _evaluate(client, "db.delete_records", {"count": 5})
    _evaluate(client, "email.send", {"to": "partner@external.com"})
    _evaluate(client, "email.send", {"to": "bob@acme-corp.com"})
    _evaluate(client, "file.read", {"path": "/srv/confidential/q3.pdf"})

    entries = client.get("/v1/audit", headers={"x-api-key": API_KEY}).json()["entries"]

    assert len(entries) == 5
    assert {e["effect"] for e in entries} == {
        "block",
        "allow",
        "require_hitl",
        "log_and_allow",
    }


def test_audit_entry_explains_the_decision(client: TestClient) -> None:
    """A record must carry why, not merely what -- matched rules, derived facts, and
    the policy version in force."""
    _evaluate(client, "db.delete_records", {"table": "users", "count": 500})

    entry = client.get("/v1/audit", headers={"x-api-key": API_KEY}).json()["entries"][0]

    assert entry["effect"] == "block"
    assert entry["tool"] == "db.delete_records"
    assert [r["rule_id"] for r in entry["matched_rules"]] == ["db-bulk-delete"]
    assert entry["derived"]["record_count"] == 500
    assert entry["bundle_version"] >= 1
    assert entry["hash"] and entry["prev_hash"]


def test_audit_can_be_filtered_by_outcome(client: TestClient) -> None:
    _evaluate(client, "db.delete_records", {"count": 500})
    _evaluate(client, "db.delete_records", {"count": 1})

    blocked = client.get(
        "/v1/audit", params={"effect": "block"}, headers={"x-api-key": API_KEY}
    ).json()

    assert blocked["count"] == 1
    assert blocked["entries"][0]["effect"] == "block"


def test_unknown_paths_are_recorded(client: TestClient) -> None:
    """Repeated unknowns mean a policy is applying more conservatively than intended,
    which is worth surfacing rather than hiding."""
    _evaluate(client, "db.delete_records", {"table": "users", "where": "1=1"})

    entry = client.get("/v1/audit", headers={"x-api-key": API_KEY}).json()["entries"][0]

    assert entry["effect"] == "block"
    assert "derived.record_count" in entry["unknown_paths"]
    assert entry["derived"]["record_count"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# Hash chain
# ---------------------------------------------------------------------------


def test_chain_verifies_after_many_writes(client: TestClient) -> None:
    for i in range(25):
        _evaluate(client, "db.delete_records", {"count": i})

    result = client.get("/v1/audit/verify", headers={"x-api-key": API_KEY}).json()

    assert result["chain_valid"] is True
    assert result["records_checked"] == 25
    assert result["broken_at_seq"] is None


def test_empty_chain_is_valid(client: TestClient) -> None:
    """Nothing written means nothing tampered with."""
    result = client.get("/v1/audit/verify", headers={"x-api-key": API_KEY}).json()

    assert result["chain_valid"] is True
    assert result["records_checked"] == 0


def test_sequence_numbers_are_contiguous(client: TestClient) -> None:
    for _ in range(5):
        _evaluate(client, "file.read", {"path": "/tmp/x"})

    entries = client.get("/v1/audit", headers={"x-api-key": API_KEY}).json()["entries"]
    seqs = sorted(e["seq"] for e in entries)

    assert seqs == [1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# Tenant isolation and dry run
# ---------------------------------------------------------------------------


def test_tenant_comes_from_the_api_key_not_the_request_body(client: TestClient) -> None:
    """Otherwise any authenticated caller could write into another tenant's chain by
    editing one field -- trivial cross-tenant forgery."""
    response = _evaluate(client, "file.read", {"path": "/tmp/x"}, tenant_id="someone-elses-tenant")

    assert response.status_code == 200
    listing = client.get("/v1/audit", headers={"x-api-key": API_KEY}).json()
    assert listing["tenant_id"] == TENANT
    assert listing["count"] == 1


def test_dry_run_is_evaluated_and_recorded(client: TestClient) -> None:
    """Dry run changes what the caller does, not whether the engine decides or records."""
    body = _evaluate(client, "db.delete_records", {"count": 500}, dry_run=True).json()

    assert body["decision"] == "block"
    assert body["dry_run"] is True

    entry = client.get("/v1/audit", headers={"x-api-key": API_KEY}).json()["entries"][0]
    assert entry["dry_run"] is True


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


def test_malformed_envelope_is_rejected(client: TestClient) -> None:
    response = client.post("/v1/evaluate", json={"agent_id": "a"}, headers={"x-api-key": API_KEY})

    assert response.status_code == 422


def test_unexpected_fields_are_rejected(client: TestClient) -> None:
    """extra='forbid' catches a misspelled field rather than silently ignoring it --
    a typo'd `dry_run` would otherwise execute a real action."""
    response = client.post(
        "/v1/evaluate",
        json={
            "agent_id": "a",
            "session_id": "s",
            "tool": "file.read",
            "dryrun": True,
        },
        headers={"x-api-key": API_KEY},
    )

    assert response.status_code == 422
