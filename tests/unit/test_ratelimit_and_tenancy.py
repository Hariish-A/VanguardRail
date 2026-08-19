"""Rate limiting, and the tenant boundary.

Two separate claims, both load-bearing, both easy to believe without evidence:

* the limiter actually refuses, actually recovers, and does not silently become a no-op
* one tenant cannot read, resolve, or influence another tenant's anything

The tenancy tests matter more. Every read path takes its tenant from the verified API key
rather than the request, and a single place that forgot to would be a cross-tenant data
leak that no other test in this repo would notice.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from guardrail_service import auth, dependencies
from guardrail_service.ratelimit import InProcessRateLimiter, TokenBucket

# ---------------------------------------------------------------------------
# Token bucket
# ---------------------------------------------------------------------------


def test_a_bucket_permits_a_burst_up_to_capacity() -> None:
    """Batch agents dispatch several tool calls back to back. Throttling that is
    indistinguishable from a broken guardrail."""
    bucket = TokenBucket(capacity=10, rate=1, now=0.0)

    assert all(bucket.consume(now=0.0).allowed for _ in range(10))


def test_a_bucket_refuses_once_it_is_empty() -> None:
    bucket = TokenBucket(capacity=3, rate=1, now=0.0)
    for _ in range(3):
        bucket.consume(now=0.0)

    verdict = bucket.consume(now=0.0)

    assert verdict.allowed is False
    assert verdict.retry_after == pytest.approx(1.0)


def test_a_bucket_refills_over_time() -> None:
    """A limiter that never recovers is an outage with extra steps."""
    bucket = TokenBucket(capacity=5, rate=2, now=0.0)
    for _ in range(5):
        bucket.consume(now=0.0)
    assert bucket.consume(now=0.0).allowed is False

    assert bucket.consume(now=1.0).allowed is True, "2 tokens/sec should restore one by t=1"


def test_refill_never_exceeds_capacity() -> None:
    """Otherwise an idle tenant banks unlimited capacity and spends it in one burst --
    exactly the spike the limiter exists to prevent."""
    bucket = TokenBucket(capacity=5, rate=100, now=0.0)

    assert all(bucket.consume(now=3600.0).allowed for _ in range(5))
    assert bucket.consume(now=3600.0).allowed is False


def test_retry_after_says_how_long_to_wait() -> None:
    """So a client backs off by a useful amount instead of guessing."""
    bucket = TokenBucket(capacity=1, rate=0.5, now=0.0)
    bucket.consume(now=0.0)

    assert bucket.consume(now=0.0).retry_after == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Per-tenant limiter
# ---------------------------------------------------------------------------


def test_one_tenant_cannot_exhaust_anothers_budget() -> None:
    """The whole point of keying the bucket on the tenant."""
    limiter = InProcessRateLimiter(per_minute=60, burst=3)

    for _ in range(3):
        assert limiter.check("noisy").allowed
    assert limiter.check("noisy").allowed is False

    assert limiter.check("quiet").allowed is True


def test_a_zero_limit_disables_rather_than_refusing_everything() -> None:
    """A config typo that reads as "zero requests permitted" would take the service down
    entirely, which is a spectacular failure mode for a throughput setting."""
    limiter = InProcessRateLimiter(per_minute=0)

    assert all(limiter.check("anyone").allowed for _ in range(1000))


def test_the_tenant_table_is_bounded() -> None:
    """Unbounded growth here would turn a rate limiter into a memory-exhaustion vector,
    which would be an unusually ironic denial of service."""
    limiter = InProcessRateLimiter(per_minute=60, max_tenants=10)

    for i in range(500):
        limiter.check(f"tenant-{i}")

    assert len(limiter._buckets) <= 10


def test_evicting_a_tenant_fails_open_not_closed() -> None:
    """Eviction hands back a fresh bucket. That is deliberate: the failure mode of the
    cap must be permissiveness, never a wrongly refused request."""
    limiter = InProcessRateLimiter(per_minute=60, burst=1, max_tenants=2)

    assert limiter.check("a").allowed is True
    assert limiter.check("a").allowed is False

    limiter.check("b")
    limiter.check("c")  # evicts the least-recently-used

    assert limiter.check("a").allowed is True


def test_the_global_ceiling_is_reported_honestly() -> None:
    """In-process limiting means the real ceiling is containers x per-container rate.
    A limiter documented as 600/min while permitting N x 600/min would be a security
    control that misrepresents itself."""
    limiter = InProcessRateLimiter(per_minute=600)

    assert limiter.global_ceiling(reserved_concurrency=10) == 6000
    assert limiter.global_ceiling(reserved_concurrency=1) == 600


# ---------------------------------------------------------------------------
# Over HTTP
# ---------------------------------------------------------------------------

KEY_A = "tenant-a-key"
KEY_B = "tenant-b-key"


@pytest.fixture
def _wire(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Two tenants, and rate limiting switched ON -- the suite default is off."""
    key_table = {
        auth.hash_key(KEY_A): {"key_id": "a-1", "tenant_id": "tenant-a", "name": "A"},
        auth.hash_key(KEY_B): {"key_id": "b-1", "tenant_id": "tenant-b", "name": "B"},
    }
    monkeypatch.setenv("GUARDRAIL_API_KEYS_JSON", json.dumps(key_table))
    monkeypatch.setenv("GUARDRAIL_STAGE", "local")
    monkeypatch.setenv("GUARDRAIL_RATE_LIMIT_PER_MINUTE", "600")
    monkeypatch.setenv("GUARDRAIL_RATE_LIMIT_BURST", "3")

    from guardrail_service.config import get_settings

    auth.reset_key_cache()
    get_settings.cache_clear()
    dependencies.reset_caches()
    yield
    auth.reset_key_cache()
    get_settings.cache_clear()
    dependencies.reset_caches()


@pytest.fixture
def client(_wire: None) -> TestClient:
    from guardrail_service.app import app

    return TestClient(app)


def _evaluate(client: TestClient, key: str, **extra: Any) -> Any:
    body = {
        "agent_id": "bot",
        "session_id": "s1",
        "tool": "file.read",
        "arguments": {"path": "/tmp/x"},
        **extra,
    }
    return client.post("/v1/evaluate", json=body, headers={"x-api-key": key})


def test_a_flood_is_refused_with_429_and_retry_after(client: TestClient) -> None:
    statuses = [_evaluate(client, KEY_A).status_code for _ in range(6)]

    assert statuses[:3] == [200, 200, 200], statuses
    assert 429 in statuses, statuses

    throttled = _evaluate(client, KEY_A)
    assert throttled.status_code == 429
    assert int(throttled.headers["retry-after"]) >= 1


def test_throttling_one_tenant_does_not_affect_another(client: TestClient) -> None:
    """A shared bucket would let one noisy agent deny service to every other customer."""
    for _ in range(6):
        _evaluate(client, KEY_A)

    assert _evaluate(client, KEY_A).status_code == 429
    assert _evaluate(client, KEY_B).status_code == 200


def test_the_limit_is_charged_after_authentication(client: TestClient) -> None:
    """Charging before the key is verified would key the budget off something an attacker
    controls, and let an unauthenticated flood exhaust a real tenant's allowance."""
    for _ in range(20):
        assert client.post("/v1/evaluate", json={}).status_code == 401

    assert _evaluate(client, KEY_A).status_code == 200


def test_remaining_budget_is_reported(client: TestClient) -> None:
    response = _evaluate(client, KEY_A)

    assert "x-ratelimit-remaining" in response.headers


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


def test_the_audit_log_is_scoped_to_the_callers_tenant(client: TestClient) -> None:
    _evaluate(client, KEY_A)
    _evaluate(client, KEY_A)
    _evaluate(client, KEY_B)

    a = client.get("/v1/audit", headers={"x-api-key": KEY_A}).json()
    b = client.get("/v1/audit", headers={"x-api-key": KEY_B}).json()

    assert a["tenant_id"] == "tenant-a" and a["count"] == 2
    assert b["tenant_id"] == "tenant-b" and b["count"] == 1


def test_the_tenant_cannot_be_overridden_from_the_request_body(client: TestClient) -> None:
    """Otherwise any authenticated caller writes into another tenant's hash chain by
    editing one field -- trivial cross-tenant forgery."""
    _evaluate(client, KEY_A, tenant_id="tenant-b")

    assert client.get("/v1/audit", headers={"x-api-key": KEY_B}).json()["count"] == 0
    assert client.get("/v1/audit", headers={"x-api-key": KEY_A}).json()["count"] == 1


def test_each_tenant_has_an_independent_hash_chain(client: TestClient) -> None:
    """Sequence numbers restart per tenant, so one tenant's volume cannot be inferred
    from another's audit records."""
    first_a = _evaluate(client, KEY_A).json()
    first_b = _evaluate(client, KEY_B).json()

    assert first_a["audit_seq"] == 1
    assert first_b["audit_seq"] == 1


def test_a_pending_decision_is_invisible_to_another_tenant(client: TestClient) -> None:
    held = _evaluate(client, KEY_A, tool="email.send", arguments={"to": ["x@external.com"]}).json()
    decision_id = held["hitl"]["decision_id"]

    assert client.get("/v1/decisions", headers={"x-api-key": KEY_B}).json()["decisions"] == []
    assert (
        client.get(f"/v1/decisions/{decision_id}", headers={"x-api-key": KEY_B}).status_code == 404
    )


def test_another_tenant_cannot_resolve_a_held_decision(client: TestClient) -> None:
    """The one that would matter most: approving another tenant's blocked action."""
    held = _evaluate(client, KEY_A, tool="email.send", arguments={"to": ["x@external.com"]}).json()
    decision_id = held["hitl"]["decision_id"]

    hijack = client.post(
        f"/v1/decisions/{decision_id}/resolve",
        json={"approve": True, "reviewer": "attacker", "reason": "hijack"},
        headers={"x-api-key": KEY_B},
    )

    assert hijack.status_code == 404

    still_pending = client.get(f"/v1/decisions/{decision_id}", headers={"x-api-key": KEY_A}).json()
    assert still_pending["status"] == "pending"
    assert still_pending["allows_execution"] is False


def test_policy_is_scoped_per_tenant(client: TestClient) -> None:
    """Reading another tenant's published policy would disclose their controls, which is
    a map of what they do not defend."""
    listing_a = client.get("/v1/policies", headers={"x-api-key": KEY_A}).json()
    listing_b = client.get("/v1/policies", headers={"x-api-key": KEY_B}).json()

    assert listing_a["tenant_id"] == "tenant-a"
    assert listing_b["tenant_id"] == "tenant-b"


def test_simulation_resolves_the_callers_own_policy(client: TestClient) -> None:
    """Simulation writes nothing, so it would be an inviting way to read another
    tenant's rules back one decision at a time."""
    response = client.post(
        "/v1/simulate",
        json={
            "action": {
                "agent_id": "bot",
                "session_id": "s",
                "tool": "file.read",
                "arguments": {"path": "/tmp/x"},
                "tenant_id": "tenant-b",
            }
        },
        headers={"x-api-key": KEY_A},
    )

    assert response.status_code == 200
    assert response.json()["bundle_source"] == "active"
