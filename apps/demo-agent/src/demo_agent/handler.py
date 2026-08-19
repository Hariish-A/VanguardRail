"""The agent, running **on AWS**, governed by the control plane **on AWS**.

This closes the last gap against the brief. Until now the control plane was deployed and
the agent that exercised it ran on a laptop against local Ollama, so the honest claim was
"Guardrail governs an agent running outside AWS". Here the agent is a Lambda of its own:

    POST /  ──►  agent Lambda  ──HTTPS──►  guardrail Lambda  ──►  DynamoDB audit chain
                      │
                      └──HTTPS──►  Groq (hosted inference)

Nothing about the guardrail changes. The agent is an ordinary SDK consumer, which is the
point: if governing an AWS-hosted agent had required special support in the control plane,
the integration story would be much weaker than it is.

## Why Groq rather than a tunnel back to a laptop

The alternative was `cloudflared` exposing local Ollama to the agent Lambda. That works,
but the demo then only runs while a particular laptop is awake, and it publishes an
unauthenticated Ollama to the internet. Groq's free tier removes both problems: the demo
works whenever anyone runs it, and no personal machine is involved. Ollama remains the
default for local runs, and neither path required a code change -- the provider layer is
configured by environment variable.

## Why this endpoint is authenticated

Every invocation spends Groq quota. A public unauthenticated endpoint that burns someone
else's rate limit is a denial-of-wallet waiting to happen, so a key is required. It is
compared as a SHA-256 digest in constant time, exactly as the control plane does it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import traceback
from typing import Any

API_KEY_HEADER = "x-api-key"


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _authorised(event: dict[str, Any]) -> bool:
    """Constant-time check of the presented key against a stored digest.

    Returns False when no digest is configured. Refusing by default matters: a deploy
    that forgot the variable would otherwise expose an endpoint that spends money on
    every call to anyone who finds it.
    """
    expected = os.environ.get("GUARDRAIL_AGENT_KEY_SHA256", "").strip()
    if not expected:
        return False

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    presented = headers.get(API_KEY_HEADER, "")
    if not presented:
        return False

    digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, expected)


def _describe() -> dict[str, Any]:
    """What this deployment is, for a GET. Never echoes a credential."""
    from demo_agent.llm import LLMConfig

    config = LLMConfig()
    return {
        "service": "guardrail-demo-agent",
        "hosted_on": "aws-lambda",
        "governed_by": os.environ.get("GUARDRAIL_BASE_URL", "(unset)"),
        "llm": {
            "provider": "groq" if "groq" in config.base_url else "openai-compatible",
            "base_url": config.base_url,
            "model": config.model,
            "api_key_configured": bool(config.api_key and config.api_key != "ollama"),
        },
        "usage": {
            "method": "POST",
            "headers": {API_KEY_HEADER: "<agent key>"},
            "body": {"task": "delete all 500 inactive user accounts", "dry_run": False},
        },
    }


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Run one task end to end and return the full transcript.

    The transcript is the deliverable, not the prose: it names every tool the model tried,
    what policy decided, which rule fired, and the audit sequence number -- so a reader can
    go and find the same decision in `/v1/audit` afterwards.
    """
    method = (
        (event.get("requestContext") or {}).get("http", {}).get("method", "POST")
        if isinstance(event.get("requestContext"), dict)
        else "POST"
    )

    if method == "GET":
        return _response(200, _describe())

    if not _authorised(event):
        return _response(
            401,
            {
                "error": "unauthorized",
                "detail": (
                    f"Missing or invalid {API_KEY_HEADER}. This endpoint spends hosted "
                    "inference quota, so it is not open."
                ),
            },
        )

    try:
        raw = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64

            raw = base64.b64decode(raw).decode("utf-8")
        payload = json.loads(raw) if raw.strip() else {}
    except (ValueError, TypeError) as exc:
        return _response(400, {"error": "bad_request", "detail": f"body is not JSON: {exc}"})

    task = str(payload.get("task", "")).strip()
    if not task:
        return _response(
            400,
            {"error": "bad_request", "detail": 'give the agent a task, e.g. {"task": "..."}'},
        )

    for name in ("GUARDRAIL_BASE_URL", "GUARDRAIL_API_KEY"):
        if not os.environ.get(name):
            return _response(
                500, {"error": "misconfigured", "detail": f"{name} is not set on this function"}
            )

    from guardrail_sdk import GuardrailClient, set_client

    from demo_agent.agent import Agent
    from demo_agent.llm import LLMConfig, LLMProvider
    from demo_agent.tools import SIDE_EFFECTS, reset_side_effects

    # **Load-bearing.** SIDE_EFFECTS is a module-level list and Lambda reuses warm
    # containers, so without this the second invocation reports the first one's actions
    # as its own -- which in a governance demo would be an outright false transcript.
    reset_side_effects()

    started = time.perf_counter()
    try:
        client = GuardrailClient(fail_open=bool(payload.get("fail_open", False)))
        # Installs the client every @governed_tool reads. Omitting it makes every tool
        # refuse to run rather than run ungoverned -- fail-closed even against this bug.
        set_client(client)

        agent = Agent(
            LLMProvider(LLMConfig()),
            agent_id=str(payload.get("agent_id", "aws-ops-assistant")),
            max_turns=int(payload.get("max_turns", 4)),
            dry_run=bool(payload.get("dry_run", False)),
        )
        run = agent.run(task, session_id=payload.get("session_id"))
    except Exception as exc:
        return _response(
            502,
            {
                "error": "agent_failed",
                "detail": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc()[-1500:],
            },
        )

    return _response(
        200,
        {
            "task": run.task,
            "session_id": run.session_id,
            "turns": run.turns,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "hosted_on": "aws-lambda",
            "governed_by": os.environ.get("GUARDRAIL_BASE_URL"),
            "model": LLMConfig().model,
            "tool_calls": [
                {
                    "tool": o.tool,
                    "arguments": o.arguments,
                    "status": o.status,
                    "policy_rules": o.rule_ids,
                    "detail": o.detail,
                    "decision_id": o.decision_id,
                    "audit_seq": o.audit_seq,
                }
                for o in run.outcomes
            ],
            # The ledger is what makes a blocked action verifiable rather than merely
            # reported: it lists what actually happened, so "blocked" can be checked
            # against "nothing was done" instead of taken on trust.
            "side_effects": [{"tool": s.tool, "detail": s.detail} for s in SIDE_EFFECTS],
            "summary": {
                "executed": len(run.executed),
                "blocked": len(run.blocked),
                "held_for_review": len(run.pending),
            },
            "final_message": run.final_message,
        },
    )
