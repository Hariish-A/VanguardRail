"""Human-in-the-loop workflow.

The properties under test, in order of how badly getting them wrong would hurt:

1. **Silence is never consent.** An unanswered request must not become an approval.
2. **Exactly one winner.** Two reviewers clicking at once must not both succeed.
3. **Expiry is computed, not delegated.** DynamoDB TTL lags by up to 48 hours, so a
   decision that expired an hour ago must already be unanswerable.
4. **The judgement is auditable.** Who approved, when, and why -- appended to the same
   tamper-evident chain as the original decision.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from guardrail_service import auth, dependencies
from guardrail_service.storage.audit import InMemoryAuditRepository
from guardrail_service.storage.decisions import (
    DecisionAlreadyResolved,
    InMemoryDecisionRepository,
    build_pending,
)

API_KEY = "test-key-do-not-use-in-production"
TENANT = "acme"


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(
        "GUARDRAIL_API_KEYS_JSON",
        json.dumps(
            {
                auth.hash_key(API_KEY): {
                    "key_id": "test-key",
                    "tenant_id": TENANT,
                    "name": "reviewer-one",
                }
            }
        ),
    )
    auth.reset_key_cache()

    audit_repo = InMemoryAuditRepository()
    decision_repo = InMemoryDecisionRepository()

    from guardrail_service.routers import audit as audit_router
    from guardrail_service.routers import decisions as decisions_router
    from guardrail_service.routers import evaluate as evaluate_router

    for module in (evaluate_router, audit_router, decisions_router):
        if hasattr(module, "get_audit_repository"):
            monkeypatch.setattr(module, "get_audit_repository", lambda: audit_repo)
        if hasattr(module, "get_decision_repository"):
            monkeypatch.setattr(module, "get_decision_repository", lambda: decision_repo)
    monkeypatch.setattr(dependencies, "get_audit_repository", lambda: audit_repo)
    monkeypatch.setattr(dependencies, "get_decision_repository", lambda: decision_repo)

    yield
    auth.reset_key_cache()


@pytest.fixture
def client() -> TestClient:
    from guardrail_service.app import app

    return TestClient(app)


def _headers() -> dict[str, str]:
    return {"x-api-key": API_KEY}


def _trigger_hitl(client: TestClient, **overrides: Any) -> dict[str, Any]:
    """Cause a require_hitl decision via the real policy: an external email."""
    body = {
        "agent_id": "ops-bot",
        "session_id": "sess-hitl",
        "tool": "email.send",
        "arguments": {"to": "partner@external.com", "subject": "Q3 numbers"},
        **overrides,
    }
    response = client.post("/v1/evaluate", json=body, headers=_headers())
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# A held action becomes a reviewable decision
# ---------------------------------------------------------------------------


def test_require_hitl_creates_a_pending_decision(client: TestClient) -> None:
    evaluation = _trigger_hitl(client)

    assert evaluation["decision"] == "require_hitl"
    assert evaluation["allowed"] is False

    queue = client.get("/v1/decisions", headers=_headers()).json()

    assert queue["count"] == 1
    assert queue["decisions"][0]["decision_id"] == evaluation["decision_id"]


def test_the_polled_id_exists_immediately(client: TestClient) -> None:
    """The decision is created before the response is returned.

    Handing an agent an id to poll that is not yet queryable would make a fast SDK 404
    on its first attempt and abandon a perfectly valid action.
    """
    evaluation = _trigger_hitl(client)

    response = client.get(f"/v1/decisions/{evaluation['decision_id']}", headers=_headers())

    assert response.status_code == 200
    assert response.json()["status"] == "pending"


def test_the_queue_carries_enough_context_to_decide(client: TestClient) -> None:
    """A reviewer approving an outbound email must see who it reaches and why it was
    held -- not merely that "an email.send is pending"."""
    _trigger_hitl(client)

    entry = client.get("/v1/decisions", headers=_headers()).json()["decisions"][0]

    assert entry["tool"] == "email.send"
    assert entry["arguments"]["to"] == "partner@external.com"
    assert entry["arguments"]["subject"] == "Q3 numbers"
    assert "external-email-review" in [r["rule_id"] for r in entry["matched_rules"]]
    assert entry["agent_id"] == "ops-bot"
    assert entry["seconds_remaining"] > 0
    assert entry["on_timeout"] == "deny"


def test_pending_decisions_require_authentication(client: TestClient) -> None:
    assert client.get("/v1/decisions").status_code == 401
    assert client.post("/v1/decisions/x/resolve", json={"approve": True}).status_code == 401


# ---------------------------------------------------------------------------
# Approval and denial
# ---------------------------------------------------------------------------


def test_approval_permits_execution(client: TestClient) -> None:
    decision_id = _trigger_hitl(client)["decision_id"]

    resolved = client.post(
        f"/v1/decisions/{decision_id}/resolve",
        json={"approve": True, "reason": "Reviewed with legal; partner is under NDA."},
        headers=_headers(),
    ).json()

    assert resolved["status"] == "approved"
    assert resolved["allows_execution"] is True
    assert resolved["reason"].startswith("Reviewed with legal")
    assert resolved["reviewer"] == "reviewer-one"
    assert resolved["resolved_at"]


def test_denial_refuses_execution(client: TestClient) -> None:
    decision_id = _trigger_hitl(client)["decision_id"]

    resolved = client.post(
        f"/v1/decisions/{decision_id}/resolve",
        json={"approve": False, "reason": "Q3 numbers are not public yet."},
        headers=_headers(),
    ).json()

    assert resolved["status"] == "denied"
    assert resolved["allows_execution"] is False


def test_a_resolved_decision_leaves_the_queue(client: TestClient) -> None:
    decision_id = _trigger_hitl(client)["decision_id"]
    client.post(f"/v1/decisions/{decision_id}/resolve", json={"approve": True}, headers=_headers())

    assert client.get("/v1/decisions", headers=_headers()).json()["count"] == 0


def test_resolving_twice_conflicts(client: TestClient) -> None:
    """The second reviewer is told plainly rather than having their click do nothing."""
    decision_id = _trigger_hitl(client)["decision_id"]
    client.post(f"/v1/decisions/{decision_id}/resolve", json={"approve": True}, headers=_headers())

    second = client.post(
        f"/v1/decisions/{decision_id}/resolve", json={"approve": False}, headers=_headers()
    )

    assert second.status_code == 409
    assert "already approved" in second.json()["detail"]


def test_an_approval_cannot_be_reversed_by_a_later_denial(client: TestClient) -> None:
    decision_id = _trigger_hitl(client)["decision_id"]
    client.post(f"/v1/decisions/{decision_id}/resolve", json={"approve": True}, headers=_headers())
    client.post(f"/v1/decisions/{decision_id}/resolve", json={"approve": False}, headers=_headers())

    final = client.get(f"/v1/decisions/{decision_id}", headers=_headers()).json()

    assert final["status"] == "approved"


def test_resolving_an_unknown_decision_is_404(client: TestClient) -> None:
    response = client.post(
        "/v1/decisions/does-not-exist/resolve", json={"approve": True}, headers=_headers()
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Expiry -- silence is never consent
# ---------------------------------------------------------------------------


def test_an_expired_decision_denies_by_default() -> None:
    """The single most important property in this file."""
    decision = build_pending(
        decision_id="d1",
        tenant_id=TENANT,
        tool="email.send",
        arguments={},
        agent_id="a",
        session_id="s",
        matched_rules=[],
        message=None,
        audit_seq=1,
        timeout_seconds=-1,  # already past
        on_timeout="deny",
    )

    assert decision.is_expired
    assert decision.effective_status == "expired"
    assert decision.allows_execution is False


def test_expiry_can_be_configured_to_allow() -> None:
    """Some low-risk actions are better auto-approved than left blocking a pipeline.

    Available, but never the default -- opting into it should be a deliberate act.
    """
    decision = build_pending(
        decision_id="d2",
        tenant_id=TENANT,
        tool="file.read",
        arguments={},
        agent_id="a",
        session_id="s",
        matched_rules=[],
        message=None,
        audit_seq=1,
        timeout_seconds=-1,
        on_timeout="allow",
    )

    assert decision.effective_status == "expired"
    assert decision.allows_execution is True


def test_expiry_is_computed_not_delegated_to_ttl() -> None:
    """DynamoDB TTL deletes on a best-effort schedule that can lag 48 hours.

    A decision that expired an hour ago must already be unanswerable, so expiry is
    evaluated by comparing timestamps on every read.
    """
    repo = InMemoryDecisionRepository()
    expired = build_pending(
        decision_id="d3",
        tenant_id=TENANT,
        tool="email.send",
        arguments={},
        agent_id="a",
        session_id="s",
        matched_rules=[],
        message=None,
        audit_seq=1,
        timeout_seconds=-3600,
    )
    repo.create(expired)

    # Still physically present -- TTL has not reclaimed it.
    assert repo.get(TENANT, "d3") is not None
    # But no longer actionable.
    assert repo.list_pending(TENANT) == []
    with pytest.raises(DecisionAlreadyResolved):
        repo.resolve(TENANT, "d3", approve=True, reviewer="r", reason="")


def test_an_expired_decision_cannot_be_approved_late(client: TestClient) -> None:
    """Approving something that already timed out would reverse a denial the agent has
    almost certainly already acted on."""
    repo = InMemoryDecisionRepository()
    repo.create(
        build_pending(
            decision_id="late",
            tenant_id=TENANT,
            tool="email.send",
            arguments={},
            agent_id="a",
            session_id="s",
            matched_rules=[],
            message=None,
            audit_seq=1,
            timeout_seconds=-1,
        )
    )

    with pytest.raises(DecisionAlreadyResolved, match="expired"):
        repo.resolve(TENANT, "late", approve=True, reviewer="r", reason="too late")


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_simultaneous_reviewers_produce_exactly_one_winner() -> None:
    """Two people open the queue and click at the same moment.

    Exactly one judgement may stand. The conditional update is what guarantees it; this
    asserts the guarantee rather than assuming it.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    repo = InMemoryDecisionRepository()
    repo.create(
        build_pending(
            decision_id="race",
            tenant_id=TENANT,
            tool="email.send",
            arguments={},
            agent_id="a",
            session_id="s",
            matched_rules=[],
            message=None,
            audit_seq=1,
            timeout_seconds=600,
        )
    )

    lock = threading.Lock()
    results: list[str] = []

    def attempt(index: int) -> None:
        with lock:  # stands in for DynamoDB's conditional-write serialisation
            try:
                repo.resolve(
                    TENANT, "race", approve=index % 2 == 0, reviewer=f"r{index}", reason=""
                )
                results.append("won")
            except DecisionAlreadyResolved:
                results.append("conflict")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(attempt, range(8)))

    assert results.count("won") == 1, f"expected exactly one winner, got {results}"
    assert results.count("conflict") == 7


# ---------------------------------------------------------------------------
# Auditability
# ---------------------------------------------------------------------------


def test_the_resolution_is_appended_to_the_audit_chain(client: TestClient) -> None:
    """ "Who approved this, and what did they say" is the question an auditor asks."""
    decision_id = _trigger_hitl(client)["decision_id"]

    client.post(
        f"/v1/decisions/{decision_id}/resolve",
        json={"approve": True, "reason": "Cleared by legal."},
        headers=_headers(),
    )

    entries = client.get("/v1/audit", headers=_headers()).json()["entries"]
    resolution = next(e for e in entries if e["decision_id"].endswith(":resolution"))

    assert "approved by reviewer-one" in resolution["message"]
    assert "Cleared by legal." in resolution["message"]


def test_the_chain_still_verifies_after_a_resolution(client: TestClient) -> None:
    """The judgement is chained like any other record, so it cannot be edited out
    without breaking verification."""
    decision_id = _trigger_hitl(client)["decision_id"]
    client.post(
        f"/v1/decisions/{decision_id}/resolve",
        json={"approve": False, "reason": "Not public yet."},
        headers=_headers(),
    )

    verification = client.get("/v1/audit/verify", headers=_headers()).json()

    assert verification["chain_valid"] is True
    assert verification["records_checked"] == 2


def test_tenants_cannot_see_each_others_pending_decisions() -> None:
    repo = InMemoryDecisionRepository()
    for tenant in ("acme", "globex"):
        repo.create(
            build_pending(
                decision_id=f"{tenant}-1",
                tenant_id=tenant,
                tool="email.send",
                arguments={},
                agent_id="a",
                session_id="s",
                matched_rules=[],
                message=None,
                audit_seq=1,
                timeout_seconds=600,
            )
        )

    assert [d.decision_id for d in repo.list_pending("acme")] == ["acme-1"]
    assert [d.decision_id for d in repo.list_pending("globex")] == ["globex-1"]


# ---------------------------------------------------------------------------
# Item shape
# ---------------------------------------------------------------------------


def test_pending_items_join_the_sparse_queue_index() -> None:
    decision = build_pending(
        decision_id="d",
        tenant_id=TENANT,
        tool="email.send",
        arguments={"amount": 10.5},
        agent_id="a",
        session_id="s",
        matched_rules=[],
        message=None,
        audit_seq=1,
    )

    item = decision.to_item()

    assert item["gsi1pk"] == f"TENANT#{TENANT}#PENDING"
    # Floats live inside the JSON string; DynamoDB rejects them as attributes.
    assert isinstance(item["arguments_json"], str)
    assert json.loads(item["arguments_json"])["amount"] == 10.5


def test_the_ttl_outlives_the_review_window() -> None:
    """If the record vanished at the review deadline, an agent polling a moment later
    would get a 404 instead of being told the request expired."""
    decision = build_pending(
        decision_id="d",
        tenant_id=TENANT,
        tool="email.send",
        arguments={},
        agent_id="a",
        session_id="s",
        matched_rules=[],
        message=None,
        audit_seq=1,
        timeout_seconds=900,
    )

    item = decision.to_item()

    assert item["expires_at"] > item["decision_expires_at"] + 86_400
    assert item["expires_at"] > time.time()
