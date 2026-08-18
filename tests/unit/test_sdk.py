"""SDK enforcement behaviour.

Uses a stub HTTP transport rather than a live service, so every branch -- including the
failure branches that are hard to trigger against a healthy deployment -- is covered
deterministically.

The most important assertions here are about what did *not* happen: a blocked tool's body
must never run. That is the difference between a guardrail and a log.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from guardrail_sdk import (
    ActionBlocked,
    AgentContext,
    ApprovalRequired,
    CircuitBreaker,
    GuardrailClient,
    GuardrailUnavailable,
    governed_tool,
    set_client,
    set_context,
)

EXECUTED: list[str] = []


@pytest.fixture(autouse=True)
def _clean() -> None:
    EXECUTED.clear()
    set_context(AgentContext(agent_id="test-agent", session_id="test-session"))


def _decision(name: str, **overrides: Any) -> dict[str, Any]:
    body = {
        "decision": name,
        "allowed": name in ("allow", "log_and_allow"),
        "matched_rules": [{"rule_id": "some-rule", "effect": name, "severity": "high"}],
        "message": f"decision was {name}",
        "decision_id": "d-1",
        "audit_seq": 7,
        "audit_hash": "abc123",
        "bundle_id": "default",
        "bundle_version": 1,
        "dry_run": False,
        "latency_ms": 1.5,
    }
    if name == "require_hitl":
        body["hitl"] = {
            "decision_id": "d-1",
            "timeout_seconds": 900,
            "on_timeout": "deny",
            "poll_url": "/v1/decisions/d-1",
        }
    body.update(overrides)
    return body


def _client(responder: Any, *, fail_open: bool = False, max_retries: int = 0) -> GuardrailClient:
    return GuardrailClient(
        base_url="https://guardrail.test",
        api_key="k",
        fail_open=fail_open,
        max_retries=max_retries,
        transport=httpx.MockTransport(responder),
    )


def _always(name: str) -> Any:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_decision(name))

    return responder


def _tool() -> Any:
    @governed_tool("db.delete_records")
    def delete(table: str, count: int = 0) -> str:
        EXECUTED.append(f"{table}:{count}")
        return "done"

    return delete


# ---------------------------------------------------------------------------
# Enforcement is pre-execution
# ---------------------------------------------------------------------------


def test_a_blocked_tool_body_never_runs() -> None:
    """The core claim of the whole project."""
    set_client(_client(_always("block")))
    delete = _tool()

    with pytest.raises(ActionBlocked) as caught:
        delete("users", 500)

    assert EXECUTED == [], "the tool executed despite being blocked"
    assert caught.value.rule_ids == ["some-rule"]


def test_a_pending_tool_body_never_runs() -> None:
    set_client(_client(_always("require_hitl")))
    delete = _tool()

    with pytest.raises(ApprovalRequired) as caught:
        delete("users", 5)

    assert EXECUTED == []
    assert caught.value.decision_id == "d-1"
    assert caught.value.decision.hitl is not None
    assert caught.value.decision.hitl.on_timeout == "deny"


def test_an_allowed_tool_runs() -> None:
    set_client(_client(_always("allow")))
    delete = _tool()

    assert delete("users", 5) == "done"
    assert EXECUTED == ["users:5"]


def test_log_and_allow_still_executes() -> None:
    """A client that treated log_and_allow as a denial would break every audited-but-
    permitted action, which is most of the useful ones."""
    set_client(_client(_always("log_and_allow")))
    delete = _tool()

    assert delete("users", 1) == "done"
    assert EXECUTED == ["users:1"]


def test_pending_can_be_treated_as_a_block_when_configured() -> None:
    set_client(_client(_always("require_hitl")))

    @governed_tool("db.delete_records", on_pending="block")
    def delete(table: str) -> str:
        EXECUTED.append(table)
        return "done"

    with pytest.raises(ActionBlocked):
        delete("users")
    assert EXECUTED == []


# ---------------------------------------------------------------------------
# Argument binding -- what the policy actually sees
# ---------------------------------------------------------------------------


def test_default_arguments_are_sent_to_the_policy() -> None:
    """Otherwise a threshold rule could be bypassed by simply omitting the parameter."""
    seen: dict[str, Any] = {}

    def responder(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_decision("allow"))

    set_client(_client(responder))

    @governed_tool("db.delete_records")
    def delete(table: str, count: int = 42) -> str:
        return "done"

    delete("users")

    assert seen["arguments"] == {"table": "users", "count": 42}


def test_keyword_and_positional_arguments_normalise_identically() -> None:
    """A policy must not be evadable by changing call style."""
    captured: list[dict[str, Any]] = []

    def responder(request: httpx.Request) -> httpx.Response:
        import json

        captured.append(json.loads(request.content)["arguments"])
        return httpx.Response(200, json=_decision("allow"))

    set_client(_client(responder))
    delete = _tool()

    delete("users", 5)
    delete(table="users", count=5)
    delete("users", count=5)

    assert captured[0] == captured[1] == captured[2]


def test_unserialisable_arguments_do_not_break_governance() -> None:
    """A tool taking a database handle must still be governable; the policy cares about
    its other arguments."""

    class Handle:
        def __repr__(self) -> str:
            return "<db handle>"

    seen: dict[str, Any] = {}

    def responder(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_decision("allow"))

    set_client(_client(responder))

    @governed_tool("db.delete_records")
    def delete(conn: Handle, table: str) -> str:
        return "done"

    delete(Handle(), "users")

    assert seen["arguments"]["table"] == "users"
    assert seen["arguments"]["conn"] == "<db handle>"


def test_agent_identity_travels_with_the_context() -> None:
    seen: dict[str, Any] = {}

    def responder(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_decision("allow"))

    set_client(_client(responder))
    set_context(AgentContext(agent_id="agent-9", session_id="sess-9", context={"env": "prod"}))
    _tool()("users", 1)

    assert seen["agent_id"] == "agent-9"
    assert seen["session_id"] == "sess-9"
    assert seen["context"] == {"env": "prod"}


# ---------------------------------------------------------------------------
# Fail-closed
# ---------------------------------------------------------------------------


def test_unreachable_guardrail_blocks_by_default() -> None:
    """A governance control that disappears under load is not a control."""

    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    set_client(_client(responder))
    delete = _tool()

    with pytest.raises(GuardrailUnavailable):
        delete("users", 5)
    assert EXECUTED == []


def test_server_error_blocks_by_default() -> None:
    set_client(_client(lambda request: httpx.Response(503, text="unavailable")))
    delete = _tool()

    with pytest.raises(GuardrailUnavailable):
        delete("users", 5)
    assert EXECUTED == []


def test_fail_open_permits_but_marks_the_decision_as_unevaluated() -> None:
    """Fail-open is a legitimate choice, but the audit story must stay honest: the
    decision came from the client's degraded mode, not from policy."""

    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = _client(responder, fail_open=True)
    set_client(client)
    delete = _tool()

    assert delete("users", 5) == "done"
    assert EXECUTED == ["users:5"]

    decision = delete.last_decision  # type: ignore[attr-defined]
    assert decision.decision_id.startswith("fail-open-")
    assert "NOT evaluated against policy" in (decision.message or "")


def test_authentication_failure_raises_even_when_failing_open() -> None:
    """A bad key is a configuration bug the operator must see, not an outage to ride
    out -- running unauthenticated silently would be worse than stopping."""
    client = _client(lambda request: httpx.Response(401, json={"detail": "nope"}), fail_open=True)

    with pytest.raises(GuardrailUnavailable, match="authentication"):
        client.evaluate(tool="db.delete_records", arguments={}, agent_id="a", session_id="s")


def test_a_tool_without_a_client_refuses_rather_than_running_ungoverned() -> None:
    from guardrail_sdk import decorator

    decorator._client.set(None)

    @governed_tool("db.delete_records")
    def delete(table: str) -> str:
        EXECUTED.append(table)
        return "done"

    with pytest.raises(RuntimeError, match="Refusing to run an ungoverned tool"):
        delete("users")
    assert EXECUTED == []


# ---------------------------------------------------------------------------
# Retries and the circuit breaker
# ---------------------------------------------------------------------------


def test_transient_failures_are_retried() -> None:
    attempts = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=_decision("allow"))

    client = _client(responder, max_retries=3)
    decision = client.evaluate(tool="db.delete_records", arguments={}, agent_id="a", session_id="s")

    assert decision.decision == "allow"
    assert attempts["n"] == 3


def test_client_errors_are_not_retried() -> None:
    """A 422 stays a 422; resending it is pure added latency on the hot path."""
    attempts = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(422, text="invalid")

    client = _client(responder, max_retries=3)

    with pytest.raises(GuardrailUnavailable):
        client.evaluate(tool="t", arguments={}, agent_id="a", session_id="s")
    assert attempts["n"] == 1


def test_retries_reuse_one_idempotency_key() -> None:
    """Otherwise a retry would be recorded as a second action, corrupting the record of
    what the agent actually did."""
    keys: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        import json

        keys.append(json.loads(request.content)["idempotency_key"])
        if len(keys) < 2:
            return httpx.Response(503)
        return httpx.Response(200, json=_decision("allow"))

    client = _client(responder, max_retries=2)
    client.evaluate(tool="t", arguments={}, agent_id="a", session_id="s")

    assert len(keys) == 2
    assert keys[0] == keys[1]


def test_circuit_breaker_opens_after_repeated_failures() -> None:
    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)

    assert breaker.is_open is False
    for _ in range(3):
        breaker.record_failure()

    assert breaker.is_open is True


def test_circuit_breaker_closes_after_the_cooldown() -> None:
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=0.01)
    breaker.record_failure()
    assert breaker.is_open is True

    import time

    time.sleep(0.02)
    assert breaker.is_open is False, "the breaker must half-open to test recovery"


def test_success_resets_the_failure_count() -> None:
    breaker = CircuitBreaker(failure_threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()

    assert breaker.is_open is False


def test_open_breaker_fails_fast_without_calling_the_service() -> None:
    """Under fail-closed this turns a multi-second timeout on every call into an
    immediate refusal -- blocked rather than hung."""
    calls = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("down")

    client = _client(responder)
    client.breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)

    with pytest.raises(GuardrailUnavailable):
        client.evaluate(tool="t", arguments={}, agent_id="a", session_id="s")
    assert calls["n"] == 1

    with pytest.raises(GuardrailUnavailable, match="circuit breaker open"):
        client.evaluate(tool="t", arguments={}, agent_id="a", session_id="s")
    assert calls["n"] == 1, "the breaker should have prevented a second network call"


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_dry_run_evaluates_but_never_executes() -> None:
    """Distinct from a block: the engine still decides and still records, so a dry run
    reports what *would* have happened."""
    seen: dict[str, Any] = {}

    def responder(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_decision("allow"))

    set_client(_client(responder))
    set_context(AgentContext(agent_id="a", session_id="s", dry_run=True))
    delete = _tool()

    assert delete("users", 5) is None
    assert EXECUTED == [], "a dry run must not touch the real world"
    assert seen["dry_run"] is True


def test_client_requires_a_url() -> None:
    import os

    saved = os.environ.pop("GUARDRAIL_BASE_URL", None)
    try:
        with pytest.raises(ValueError, match="No guardrail URL"):
            GuardrailClient(api_key="k")
    finally:
        if saved:
            os.environ["GUARDRAIL_BASE_URL"] = saved
