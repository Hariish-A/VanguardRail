"""Guardrail SDK -- enforce action policy before an agent's tool call executes.

    from guardrail_sdk import GuardrailClient, governed_tool, set_client, AgentContext

    set_client(GuardrailClient())          # reads GUARDRAIL_BASE_URL / GUARDRAIL_API_KEY

    @governed_tool("db.delete_records")
    def delete_records(table: str, count: int) -> int:
        ...

Fail-closed by default: if the guardrail cannot be reached, the tool does not run.
"""

from guardrail_sdk.client import CircuitBreaker, GuardrailClient
from guardrail_sdk.decorator import (
    AgentContext,
    get_client,
    get_context,
    governed_tool,
    last_decision,
    set_client,
    set_context,
)
from guardrail_sdk.exceptions import (
    ActionBlocked,
    ApprovalRequired,
    GuardrailError,
    GuardrailUnavailable,
)
from guardrail_sdk.models import Decision, HitlInfo, MatchedRule

__all__ = [
    "ActionBlocked",
    "AgentContext",
    "ApprovalRequired",
    "CircuitBreaker",
    "Decision",
    "GuardrailClient",
    "GuardrailError",
    "GuardrailUnavailable",
    "HitlInfo",
    "MatchedRule",
    "get_client",
    "get_context",
    "governed_tool",
    "last_decision",
    "set_client",
    "set_context",
]

__version__ = "0.1.0"
