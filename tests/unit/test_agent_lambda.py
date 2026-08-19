"""The AWS-hosted agent's Lambda handler.

Two properties carry real weight here, and both are the kind that look fine in review:

* the endpoint **refuses by default**, because every invocation spends hosted inference
  quota and an open one is a denial-of-wallet
* the side-effect ledger is **reset per invocation**, because Lambda reuses warm containers
  and a stale ledger would make the transcript actively false — reporting one run's
  actions as another's, in a system whose entire output is evidence
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest
from demo_agent.handler import lambda_handler


def _event(
    body: dict[str, Any] | None = None,
    *,
    key: str | None = None,
    method: str = "POST",
) -> dict[str, Any]:
    headers = {"content-type": "application/json"}
    if key is not None:
        headers["x-api-key"] = key
    return {
        "requestContext": {"http": {"method": method}},
        "headers": headers,
        "body": json.dumps(body) if body is not None else None,
    }


def _decoded(response: dict[str, Any]) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(response["body"])
    return parsed


AGENT_KEY = "agent-endpoint-key"
DIGEST = hashlib.sha256(AGENT_KEY.encode()).hexdigest()


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GUARDRAIL_AGENT_KEY_SHA256", DIGEST)
    monkeypatch.setenv("GUARDRAIL_BASE_URL", "https://guardrail.test")
    monkeypatch.setenv("GUARDRAIL_API_KEY", "guardrail-key")
    monkeypatch.setenv("GUARDRAIL_LLM_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("GUARDRAIL_LLM_MODEL", "qwen/qwen3.6-27b")
    monkeypatch.setenv("GUARDRAIL_LLM_API_KEY", "gsk_test")


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def test_the_endpoint_refuses_without_a_key(wired: None) -> None:
    response = lambda_handler(_event({"task": "delete everything"}))

    assert response["statusCode"] == 401


def test_the_endpoint_refuses_a_wrong_key(wired: None) -> None:
    response = lambda_handler(_event({"task": "x"}, key="not-the-key"))

    assert response["statusCode"] == 401


def test_the_endpoint_refuses_when_no_key_is_configured_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failing closed on a missing digest is the important direction.

    A deploy that forgot the variable would otherwise publish an endpoint that spends
    money for anyone who finds the URL.
    """
    monkeypatch.delenv("GUARDRAIL_AGENT_KEY_SHA256", raising=False)

    assert lambda_handler(_event({"task": "x"}, key="anything"))["statusCode"] == 401


def test_a_get_describes_the_deployment_without_leaking_the_key(wired: None) -> None:
    """Useful for a judge poking the URL, and it must not become a credential oracle."""
    body = _decoded(lambda_handler(_event(method="GET")))

    assert body["hosted_on"] == "aws-lambda"
    assert body["llm"]["model"] == "qwen/qwen3.6-27b"
    assert body["llm"]["api_key_configured"] is True

    serialized = json.dumps(body)
    assert "gsk_test" not in serialized
    assert AGENT_KEY not in serialized
    assert DIGEST not in serialized


# ---------------------------------------------------------------------------
# Request handling
# ---------------------------------------------------------------------------


def test_a_missing_task_is_a_clear_400(wired: None) -> None:
    response = lambda_handler(_event({}, key=AGENT_KEY))

    assert response["statusCode"] == 400
    assert "task" in _decoded(response)["detail"]


def test_malformed_json_is_a_400_not_a_crash(wired: None) -> None:
    event = _event(key=AGENT_KEY)
    event["body"] = "{not json"

    assert lambda_handler(event)["statusCode"] == 400


def test_a_missing_guardrail_url_is_reported_as_misconfiguration(
    wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Distinct from an agent failure. Pointing an operator at the deployment rather than
    at the model is the difference between a two-minute fix and an afternoon."""
    monkeypatch.delenv("GUARDRAIL_BASE_URL", raising=False)

    response = lambda_handler(_event({"task": "x"}, key=AGENT_KEY))

    assert response["statusCode"] == 500
    assert "GUARDRAIL_BASE_URL" in _decoded(response)["detail"]


# ---------------------------------------------------------------------------
# The warm-container trap
# ---------------------------------------------------------------------------


def test_the_side_effect_ledger_is_reset_between_invocations(
    wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The trap this handler is most likely to fall into.**

    `SIDE_EFFECTS` is a module-level list and Lambda reuses warm containers. Without an
    explicit reset the second invocation reports the first one's actions as its own — a
    false transcript in a system whose whole output is evidence.

    Simulated by pre-loading the ledger and asserting the handler clears it, which is
    exactly what a warm container looks like.
    """
    from demo_agent.tools import SIDE_EFFECTS, SideEffect

    SIDE_EFFECTS.append(SideEffect("db.delete_records", "from a previous invocation", {}))
    assert SIDE_EFFECTS

    # The LLM is unreachable here, so the run fails -- but the reset happens first, which
    # is the point: even a failed invocation must not inherit a stale ledger.
    lambda_handler(_event({"task": "anything"}, key=AGENT_KEY))

    assert SIDE_EFFECTS == [], "a stale ledger would attribute another run's actions here"


# ---------------------------------------------------------------------------
# A full governed run, with both hops stubbed
# ---------------------------------------------------------------------------


def test_a_blocked_action_is_reported_with_the_rule_and_no_side_effect(
    wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the handler: the model asks for a bulk delete, the guardrail
    blocks it, and the transcript proves nothing happened."""
    import demo_agent.handler as handler_module

    # One router, not two patches. `demo_agent.llm` and `guardrail_sdk.client` both hold
    # a reference to the *same* httpx.Client class, so patching it twice means the second
    # patch silently wins and every call -- including the LLM's -- gets the guardrail's
    # response. That is what happened on the first attempt.
    def fake_post(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        if "evaluate" in url:
            return httpx.Response(
                200,
                json={
                    "decision": "block",
                    "allowed": False,
                    "matched_rules": [
                        {"rule_id": "db-bulk-delete", "effect": 3, "severity": "critical"}
                    ],
                    "message": "Blocked: this would delete 500 records.",
                    "decision_id": "d-1",
                    "audit_seq": 42,
                    "audit_hash": "h",
                    "bundle_id": "default",
                    "bundle_version": 3,
                    "dry_run": False,
                    "latency_ms": 4.2,
                },
            )

        body = kwargs.get("json") or {}
        already_called = any(m.get("role") == "tool" for m in body.get("messages", []))
        if already_called:
            message: dict[str, Any] = {
                "role": "assistant",
                "content": "That deletion was blocked by policy db-bulk-delete.",
            }
        else:
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            # The LLM-facing schema name, not the policy name. The
                            # registry exposes `db_delete_records`; `db.delete_records`
                            # is what @governed_tool reports to the guardrail.
                            "name": "db_delete_records",
                            "arguments": json.dumps({"table": "users", "count": 500}),
                        },
                    }
                ],
            }
        return httpx.Response(200, json={"choices": [{"message": message}]})

    monkeypatch.setattr(httpx.Client, "post", fake_post, raising=True)

    response = handler_module.lambda_handler(
        _event({"task": "delete all 500 inactive users"}, key=AGENT_KEY)
    )

    assert response["statusCode"] == 200, response["body"][:400]
    body = _decoded(response)

    assert body["hosted_on"] == "aws-lambda"
    assert body["summary"]["blocked"] == 1
    assert body["summary"]["executed"] == 0
    assert body["side_effects"] == [], "a blocked action must leave no trace"

    call = body["tool_calls"][0]
    assert call["tool"] == "db.delete_records"
    assert call["status"] == "blocked"
    assert "db-bulk-delete" in call["policy_rules"]
    assert call["audit_seq"] == 42, "the transcript must point at the audit record"
