"""Exceptions the SDK raises in place of executing a tool call.

Each carries the decision that caused it, so an agent can explain the refusal to a user
rather than surfacing an opaque error. That difference is what makes a guardrail usable
instead of merely obstructive.
"""

from __future__ import annotations

from guardrail_sdk.models import Decision


class GuardrailError(Exception):
    """Base class, so callers can catch everything the SDK raises with one except."""


class ActionBlocked(GuardrailError):
    """Policy rejected the call. The wrapped function never ran."""

    def __init__(self, decision: Decision, tool: str) -> None:
        self.decision = decision
        self.tool = tool
        rules = ", ".join(r.rule_id for r in decision.matched_rules) or "policy default"
        super().__init__(
            decision.message or f"Blocked by {rules}: {tool} is not permitted by policy."
        )

    @property
    def rule_ids(self) -> list[str]:
        return [r.rule_id for r in self.decision.matched_rules]


class ApprovalRequired(GuardrailError):
    """A human must approve before this call may proceed.

    M3 adds waiting and resumption. Until then the SDK surfaces the pending decision id
    so the caller can decide what to do -- which is strictly better than treating a
    pause as a denial, because the two mean different things to the person waiting.
    """

    def __init__(self, decision: Decision, tool: str) -> None:
        self.decision = decision
        self.tool = tool
        self.decision_id = decision.decision_id
        timeout = decision.hitl.timeout_seconds if decision.hitl else None
        super().__init__(
            decision.message
            or f"{tool} is held for human review (decision {decision.decision_id})."
            + (f" Expires in {timeout}s." if timeout else "")
        )


class GuardrailUnavailable(GuardrailError):
    """The guardrail could not be reached, or could not record its decision.

    Raised only when the client is configured to fail closed, which is the default. A
    governance control that disappears under load is not a control -- so an unreachable
    guardrail blocks rather than waves the action through.
    """

    def __init__(self, reason: str, tool: str) -> None:
        self.tool = tool
        self.reason = reason
        super().__init__(
            f"Guardrail unavailable while evaluating {tool}: {reason}. "
            "Failing closed -- the action was not executed."
        )
