"""The MCP proxy and the framework adapters.

The claim these make is strong -- "governs a third-party tool server with zero changes to
it" -- so the tests are written to falsify it rather than illustrate it. The one that
matters most is `test_a_blocked_call_never_reaches_the_upstream_server`: a proxy that
forwarded first and evaluated afterwards would be an audit log with extra latency, and
every other assertion here would still pass.

A stub HTTP transport stands in for the service, so these run offline and exercise the
real client code path including its fail-closed branch.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from guardrail_sdk import GuardrailClient, MCPGuardrailProxy
from guardrail_sdk.adapters import (
    GuardedToolDispatcher,
    GuardrailCallbackHandler,
    guard_langchain_tool,
    guard_openai_tool_calls,
)
from guardrail_sdk.exceptions import ActionBlocked

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _decision(name: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "decision": name,
        "allowed": name in ("allow", "log_and_allow"),
        "matched_rules": [{"rule_id": "db-bulk-delete", "effect": name, "severity": "critical"}],
        "message": "Blocked: this would delete 500 records, above the limit of 100.",
        "decision_id": "d-1",
        "audit_seq": 7,
        "audit_hash": "abc",
        "bundle_id": "default",
        "bundle_version": 2,
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


def _client(outcome: str, *, fail_open: bool = False, status: int = 200) -> GuardrailClient:
    def responder(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, json={"detail": "boom"})
        return httpx.Response(200, json=_decision(outcome))

    return GuardrailClient(
        base_url="https://guardrail.test",
        api_key="k",
        fail_open=fail_open,
        max_retries=0,
        transport=httpx.MockTransport(responder),
    )


def _call(tool: str = "delete_file", **arguments: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 42,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments or {"path": "/etc/passwd"}},
    }


# ---------------------------------------------------------------------------
# MCP proxy -- the load-bearing behaviour
# ---------------------------------------------------------------------------


def test_a_blocked_call_never_reaches_the_upstream_server() -> None:
    """**The test that makes the proxy a control rather than a log.**

    Forwarding first and evaluating afterwards would still produce audit records, still
    report `block`, and still satisfy every other test in this file -- while the file was
    already deleted.
    """
    proxy = MCPGuardrailProxy(_client("block"), server_name="filesystem")

    result = proxy.handle_client_message(_call())

    assert result.forward is None, "a blocked call must not be forwarded upstream"
    assert result.respond is not None


def test_a_refusal_is_a_result_not_a_protocol_error() -> None:
    """MCP clients surface `isError` results to the model as readable content, and
    JSON-RPC errors as a broken server. A refusal is something to reason about."""
    proxy = MCPGuardrailProxy(_client("block"))

    respond = proxy.handle_client_message(_call()).respond
    assert respond is not None

    assert "error" not in respond, "must not be a JSON-RPC protocol error"
    assert respond["result"]["isError"] is True
    assert respond["id"] == 42, "the response must correlate with the request"
    assert respond["jsonrpc"] == "2.0"


def test_the_refusal_names_the_rule_that_caused_it() -> None:
    """'That's not allowed' invites a retry loop; naming the rule tells the agent what to
    do instead."""
    proxy = MCPGuardrailProxy(_client("block"))

    text = proxy.handle_client_message(_call()).respond["result"]["content"][0]["text"]  # type: ignore[index]

    assert "db-bulk-delete" in text
    assert "500 records" in text


def test_an_allowed_call_is_forwarded_unchanged() -> None:
    """The proxy must not rewrite traffic it permits, or it becomes a compatibility
    hazard of its own."""
    proxy = MCPGuardrailProxy(_client("allow"))
    message = _call()

    result = proxy.handle_client_message(message)

    assert result.respond is None
    assert result.forward == message


def test_everything_that_is_not_a_tool_call_passes_straight_through() -> None:
    """MCP is still moving. A proxy that only understood the methods it was written
    against would silently break a server that added one."""
    proxy = MCPGuardrailProxy(_client("block"))  # would block if it evaluated

    for method in (
        "initialize",
        "tools/list",
        "resources/list",
        "resources/read",
        "prompts/get",
        "notifications/cancelled",
        "some/future/method",
    ):
        message = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}
        result = proxy.handle_client_message(message)

        assert result.forward == message, f"{method} must pass through untouched"
        assert result.respond is None

    assert proxy.stats.evaluated == 0, "non-tool traffic must not be evaluated"


def test_tools_are_namespaced_by_server() -> None:
    """Two servers exposing `read_file` must be addressable separately, or a rule written
    for one silently governs the other."""
    seen: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["tool"])
        return httpx.Response(200, json=_decision("allow"))

    client = GuardrailClient(
        base_url="https://guardrail.test",
        api_key="k",
        max_retries=0,
        transport=httpx.MockTransport(responder),
    )
    MCPGuardrailProxy(client, server_name="filesystem").handle_client_message(_call("read_file"))

    assert seen == ["filesystem.read_file"]


def test_the_proxy_fails_closed_when_the_guardrail_is_unreachable() -> None:
    """A governance control that disappears under load is not a control."""
    proxy = MCPGuardrailProxy(_client("allow", status=503))

    result = proxy.handle_client_message(_call())

    assert result.forward is None, "must not forward when policy could not be consulted"
    text = result.respond["result"]["content"][0]["text"]  # type: ignore[index]
    assert "not performed" in text
    assert proxy.stats.blocked == 1


def test_fail_open_is_available_but_never_the_default() -> None:
    """An explicit, logged choice for teams who would rather keep agents running during
    an outage."""
    default = MCPGuardrailProxy(_client("allow", status=503))
    assert default.handle_client_message(_call()).forward is None

    opted_in = MCPGuardrailProxy(_client("allow", status=503, fail_open=True), fail_open=True)
    assert opted_in.handle_client_message(_call()).forward is not None


def test_a_held_call_is_not_forwarded_and_says_so() -> None:
    proxy = MCPGuardrailProxy(_client("require_hitl"))

    result = proxy.handle_client_message(_call())

    assert result.forward is None
    text = result.respond["result"]["content"][0]["text"]  # type: ignore[index]
    assert "HELD FOR HUMAN REVIEW" in text
    assert "d-1" in text, "the agent needs the decision id to report status"
    assert proxy.stats.held == 1


def test_non_object_arguments_are_wrapped_rather_than_rejected() -> None:
    """A server may accept scalar arguments. The policy should still see something
    addressable instead of the proxy failing."""
    proxy = MCPGuardrailProxy(_client("allow"))

    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "echo", "arguments": "just-a-string"},
    }

    assert proxy.handle_client_message(message).forward is not None


def test_stats_describe_what_the_proxy_actually_did() -> None:
    proxy = MCPGuardrailProxy(_client("block"))
    for _ in range(3):
        proxy.handle_client_message(_call("delete_file"))
    proxy.handle_client_message({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})

    assert (proxy.stats.evaluated, proxy.stats.blocked) == (3, 0 + 3)
    assert proxy.stats.blocked_tools == ["delete_file"] * 3
    assert proxy.stats.forwarded == 1


# ---------------------------------------------------------------------------
# OpenAI-compatible dispatcher
# ---------------------------------------------------------------------------


def test_a_blocked_tool_body_never_runs() -> None:
    """Detecting a bulk delete after it ran is forensics. Refusing it beforehand is a
    control."""
    ran: list[str] = []

    dispatcher = GuardedToolDispatcher(
        _client("block"), lambda name, args: ran.append(name) or "done"
    )

    result = dispatcher.call("db.delete_records", {"count": 500})

    assert ran == [], "the wrapped dispatch must not have been called"
    assert "REFUSED BY POLICY" in result
    assert "db-bulk-delete" in result


def test_an_allowed_tool_runs_and_returns_its_own_result() -> None:
    dispatcher = GuardedToolDispatcher(_client("allow"), lambda name, args: f"ran {name}")

    assert dispatcher.call("file.read", {"path": "/tmp/x"}) == "ran file.read"


def test_a_held_tool_reports_pending_without_running() -> None:
    ran: list[str] = []
    dispatcher = GuardedToolDispatcher(
        _client("require_hitl"), lambda name, args: ran.append(name) or "done"
    )

    result = dispatcher.call("email.send", {"to": ["a@b.com"]})

    assert ran == []
    assert "HELD FOR HUMAN REVIEW" in result


def test_the_dispatcher_fails_closed() -> None:
    ran: list[str] = []
    dispatcher = GuardedToolDispatcher(
        _client("allow", status=503), lambda name, args: ran.append(name) or "done"
    )

    result = dispatcher.call("db.delete_records", {"count": 5})

    assert ran == []
    assert "not performed" in result


def test_openai_tool_calls_come_back_as_tool_messages() -> None:
    """The refusal has to look like a normal tool result, or the model cannot reason
    about it and the conversation breaks."""
    calls = [
        {
            "id": "call_1",
            "function": {"name": "db.delete_records", "arguments": '{"count": 500}'},
        }
    ]

    messages = guard_openai_tool_calls(_client("block"), calls, lambda n, a: "ran")

    assert len(messages) == 1
    assert messages[0]["role"] == "tool"
    assert messages[0]["tool_call_id"] == "call_1"
    assert "REFUSED BY POLICY" in messages[0]["content"]


def test_malformed_tool_arguments_do_not_end_the_run() -> None:
    """Models emit invalid JSON. Surfacing it lets the model correct itself; raising
    would end the run over a recoverable slip."""
    calls = [{"id": "c1", "function": {"name": "x", "arguments": "{not json"}}]

    messages = guard_openai_tool_calls(_client("allow"), calls, lambda n, a: "ran")

    assert "not valid JSON" in messages[0]["content"]


def test_object_shaped_tool_calls_are_understood_too() -> None:
    """SDKs return objects; raw wire traffic is dicts. Agents in the wild carry both."""

    class Function:
        name = "file.read"
        arguments = '{"path": "/tmp/x"}'

    class Call:
        id = "call_9"
        function = Function()

    messages = guard_openai_tool_calls(_client("allow"), [Call()], lambda n, a: f"ran {n}")

    assert messages[0]["content"] == "ran file.read"
    assert messages[0]["tool_call_id"] == "call_9"


# ---------------------------------------------------------------------------
# LangChain
# ---------------------------------------------------------------------------


class _FakeTool:
    """Enough of a LangChain tool to wrap. Avoids a hard dependency in the test suite."""

    def __init__(self, name: str, func: Any) -> None:
        self.name = name
        self.func = func


def test_guarding_a_langchain_tool_stops_its_function() -> None:
    ran: list[int] = []
    tool = _FakeTool("db.delete_records", lambda count: ran.append(count) or "deleted")

    guarded = guard_langchain_tool(_client("block"), tool)
    result = guarded.func(count=500)

    assert ran == [], "the original callable must not run"
    assert "REFUSED BY POLICY" in result


def test_guarding_a_langchain_tool_lets_permitted_calls_through() -> None:
    tool = _FakeTool("file.read", lambda path: f"read {path}")

    guarded = guard_langchain_tool(_client("allow"), tool)

    assert guarded.func(path="/tmp/x") == "read /tmp/x"


def test_a_refusal_is_returned_not_raised() -> None:
    """LangChain treats a raised exception in a tool as a run failure. An agent that dies
    on a policy denial is worse than one that explains it."""
    tool = _FakeTool("db.delete_records", lambda count: "deleted")

    guarded = guard_langchain_tool(_client("block"), tool)

    result = guarded.func(count=500)  # must not raise
    assert isinstance(result, str)


def test_wrapping_a_tool_without_a_callable_is_refused_loudly() -> None:
    """Silently wrapping the wrong attribute would look governed while the original
    callable stayed reachable."""

    class Structured:
        name = "structured"

    with pytest.raises(TypeError, match="no `func` to wrap"):
        guard_langchain_tool(_client("block"), Structured())


def test_the_callback_handler_observes_and_does_not_enforce_by_default() -> None:
    """Documented limitation, asserted so it cannot quietly change.

    `on_tool_start` is a notification: LangChain gives a callback no supported way to veto
    the tool it is announcing. The handler records; `guard_langchain_tool` prevents.
    """
    handler = GuardrailCallbackHandler(_client("block"))

    handler.on_tool_start({"name": "db.delete_records"}, '{"count": 500}')

    assert len(handler.blocked) == 1, "it must still record the decision"
    # No exception: the tool would proceed. That is the limitation, not a bug.


def test_the_callback_handler_can_be_told_to_abort() -> None:
    """A blunt instrument, off by default, for hosts where aborting genuinely beats
    proceeding."""
    handler = GuardrailCallbackHandler(_client("block"), raise_on_block=True)

    with pytest.raises(ActionBlocked):
        handler.on_tool_start({"name": "db.delete_records"}, '{"count": 500}')


def test_the_callback_handler_survives_non_json_input() -> None:
    handler = GuardrailCallbackHandler(_client("allow"))

    handler.on_tool_start({"name": "search"}, "a plain string query")

    assert len(handler.decisions) == 1
