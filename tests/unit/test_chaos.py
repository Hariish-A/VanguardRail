"""Failure injection: does the system actually fail closed?

Every other test here exercises the system working. These break it on purpose, because
"fails closed" is the load-bearing safety claim of the whole product and it is the one
property that is never exercised by normal operation. A guardrail that quietly fails open
under stress is worse than no guardrail, because the organisation believes it is
protected.

Each test names the real-world incident it stands in for.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from guardrail_sdk import GuardrailClient, MCPGuardrailProxy
from guardrail_sdk.adapters import GuardedToolDispatcher
from guardrail_sdk.exceptions import GuardrailUnavailable

SIDE_EFFECTS: list[str] = []


def _tool(name: str = "db.delete_records") -> Any:
    """A tool that records the fact it ran. If it appears in SIDE_EFFECTS after a
    failure, the system failed open."""

    def run(*args: Any, **kwargs: Any) -> str:
        SIDE_EFFECTS.append(name)
        return "executed"

    return run


@pytest.fixture(autouse=True)
def _clear() -> None:
    SIDE_EFFECTS.clear()


def _client(responder: Any, *, fail_open: bool = False) -> GuardrailClient:
    return GuardrailClient(
        base_url="https://guardrail.test",
        api_key="k",
        fail_open=fail_open,
        max_retries=0,
        transport=httpx.MockTransport(responder),
    )


# ---------------------------------------------------------------------------
# The control plane is unreachable
# ---------------------------------------------------------------------------


def _network_down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused")


def _dns_failure(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("[Errno -2] Name or service not known")


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out")


def _service_down(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, json={"detail": "unavailable"})


def _audit_unavailable(request: httpx.Request) -> httpx.Response:
    """The real 503 this service returns when the audit chain cannot be written --
    the failure the load test surfaced at 5 provisioned WCU."""
    return httpx.Response(
        503,
        json={"detail": "The decision could not be recorded, so no decision is returned."},
        headers={"Retry-After": "2"},
    )


@pytest.mark.parametrize(
    ("label", "responder"),
    [
        ("network partition", _network_down),
        ("DNS failure", _dns_failure),
        ("request timeout", _timeout),
        ("service returning 503", _service_down),
        ("audit chain unwritable", _audit_unavailable),
    ],
)
def test_the_sdk_refuses_the_action_when_the_guardrail_cannot_answer(
    label: str, responder: Any
) -> None:
    """**The safety claim, under five different real outages.**

    A governance control that disappears under load is not a control. In every one of
    these the tool must not run.
    """
    client = _client(responder)

    with pytest.raises(GuardrailUnavailable):
        client.evaluate(
            tool="db.delete_records",
            arguments={"count": 5000},
            agent_id="a",
            session_id="s",
        )

    assert SIDE_EFFECTS == [], f"the tool executed despite {label}"


def test_a_governed_tool_body_never_runs_when_the_guardrail_is_down() -> None:
    """The decorator path, not just the client: it is what agent teams actually use."""
    from guardrail_sdk.decorator import AgentContext, governed_tool, set_client, set_context

    set_client(_client(_network_down))
    set_context(AgentContext(agent_id="a", session_id="s"))

    @governed_tool("db.delete_records")
    def delete(count: int) -> str:
        SIDE_EFFECTS.append("db.delete_records")
        return "deleted"

    with pytest.raises(GuardrailUnavailable):
        delete(count=5000)

    assert SIDE_EFFECTS == []


def test_the_mcp_proxy_refuses_to_forward_when_the_guardrail_is_down() -> None:
    """The proxy is the enforcement point for third-party servers, so it has to hold the
    same line -- and the upstream must never see the call."""
    proxy = MCPGuardrailProxy(_client(_network_down), server_name="filesystem")

    result = proxy.handle_client_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "/etc/shadow"}},
        }
    )

    assert result.forward is None, "the upstream server must not receive the call"
    assert "not performed" in result.respond["result"]["content"][0]["text"]  # type: ignore[index]


def test_the_openai_adapter_refuses_when_the_guardrail_is_down() -> None:
    dispatcher = GuardedToolDispatcher(_client(_network_down), lambda n, a: _tool(n)())

    result = dispatcher.call("db.delete_records", {"count": 5000})

    assert SIDE_EFFECTS == []
    assert "not performed" in result


# ---------------------------------------------------------------------------
# Degraded rather than absent
# ---------------------------------------------------------------------------


def test_a_malformed_response_is_treated_as_unavailable() -> None:
    """A 200 carrying nonsense is more dangerous than a clean failure: parsed loosely, it
    could be read as an allow. It must not be."""

    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>502 Bad Gateway</html>")

    with pytest.raises(Exception) as caught:
        _client(garbage).evaluate(
            tool="db.delete_records", arguments={"count": 5000}, agent_id="a", session_id="s"
        )

    assert not isinstance(caught.value, AssertionError)
    assert SIDE_EFFECTS == []


def test_a_response_missing_the_decision_is_not_treated_as_allow() -> None:
    """Absence of a verdict is not permission."""

    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with pytest.raises(Exception):
        _client(empty).evaluate(
            tool="db.delete_records", arguments={"count": 5000}, agent_id="a", session_id="s"
        )

    assert SIDE_EFFECTS == []


def test_an_invalid_api_key_raises_even_in_fail_open_mode() -> None:
    """Running unauthenticated is a configuration bug an operator must see, not an outage
    to ride out. Fail-open exists for outages; it must not paper over a broken deploy."""

    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Invalid API key."})

    with pytest.raises(GuardrailUnavailable, match="authentication"):
        _client(unauthorized, fail_open=True).evaluate(
            tool="db.delete_records", arguments={"count": 5}, agent_id="a", session_id="s"
        )


# ---------------------------------------------------------------------------
# Fail-open: available, explicit, and never silent
# ---------------------------------------------------------------------------


def test_fail_open_permits_the_action_but_marks_the_decision_forever() -> None:
    """Some teams would rather keep agents running through an outage. That is a legitimate
    choice, but the resulting audit story must stay honest: the decision came from the
    client's degraded mode, not from policy, and it has to be distinguishable afterwards.
    """
    decision = _client(_network_down, fail_open=True).evaluate(
        tool="db.delete_records", arguments={"count": 5000}, agent_id="a", session_id="s"
    )

    assert decision.allowed is True
    assert decision.decision_id.startswith("fail-open-")
    assert "NOT evaluated against policy" in (decision.message or "")


def test_fail_closed_is_the_default() -> None:
    """The safe posture must be what you get by omission. A control that is only safe when
    correctly configured will eventually be misconfigured."""
    client = GuardrailClient(base_url="https://guardrail.test", api_key="k")

    assert client.fail_open is False


# ---------------------------------------------------------------------------
# The circuit breaker must not become a bypass
# ---------------------------------------------------------------------------


def test_an_open_circuit_still_refuses_rather_than_permitting() -> None:
    """The breaker exists to fail *fast*, not to fail *open*.

    Under fail-closed it turns a multi-second timeout on every call into an immediate
    refusal. If it ever short-circuited to "allow" it would convert a brief outage into a
    total governance bypass -- and it would do so silently, exactly when the service is
    already unhealthy.
    """
    client = _client(_network_down)

    for _ in range(client.breaker.failure_threshold):
        with pytest.raises(GuardrailUnavailable):
            client.evaluate(tool="x", arguments={}, agent_id="a", session_id="s")

    assert client.breaker.is_open

    with pytest.raises(GuardrailUnavailable, match="circuit breaker"):
        client.evaluate(
            tool="db.delete_records", arguments={"count": 5000}, agent_id="a", session_id="s"
        )

    assert SIDE_EFFECTS == []


def test_the_breaker_recovers_so_an_outage_is_not_permanent() -> None:
    """A breaker that never closes turns a blip into an indefinite outage."""
    client = _client(_network_down)
    for _ in range(client.breaker.failure_threshold):
        with pytest.raises(GuardrailUnavailable):
            client.evaluate(tool="x", arguments={}, agent_id="a", session_id="s")

    assert client.breaker.is_open

    client.breaker.cooldown_seconds = 0
    assert client.breaker.is_open is False, "the breaker must eventually allow a probe"


# ---------------------------------------------------------------------------
# Policy store failure must not become an agent outage
# ---------------------------------------------------------------------------


def test_the_service_keeps_governing_when_the_policy_store_is_unreachable() -> None:
    """A policy-store outage must degrade to the last known good bundle, not to
    permitting everything and not to refusing everything."""
    from guardrail_core.policy import load_bundle
    from guardrail_service.policy_provider import ActivePolicyProvider

    packaged = load_bundle(
        {
            "apiVersion": "guardrail/v1",
            "metadata": {"bundle_id": "packaged", "version": 1},
            "rules": [
                {
                    "id": "db-bulk-delete",
                    "match": {
                        "tool": "db.delete_records",
                        "all": [{"path": "derived.record_count", "op": "gt", "value": 100}],
                    },
                    "effect": "block",
                }
            ],
        }
    )

    class DeadStore:
        def get_active(self, tenant_id: str, bundle_id: str) -> Any:
            raise RuntimeError("DynamoDB unreachable")

    provider = ActivePolicyProvider(DeadStore(), packaged)  # type: ignore[arg-type]
    state = provider.state("acme")

    assert state.degraded is True, "the degradation must be visible, not silent"
    assert state.bundle is packaged, "it must still be governing something"

    from guardrail_core.engine import evaluate
    from guardrail_core.models import ActionEnvelope

    result = evaluate(
        ActionEnvelope(
            agent_id="a", session_id="s", tool="db.delete_records", arguments={"count": 500}
        ),
        state.bundle,
    )
    assert result.effect.wire_name == "block", "policy must still be enforced while degraded"
