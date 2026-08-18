"""The agent loop.

An ordinary tool-calling loop, with one thing that matters: when the guardrail refuses a
call, the refusal is fed **back to the model as a tool result** rather than raised as an
error.

That single choice is what separates a usable guardrail from an obstructive one. If a
block crashes the agent, the person who asked for something gets a stack trace. If the
refusal comes back as text the model can read, the agent explains what policy stopped it
and what to do instead -- and the human learns something. The guardrail becomes part of
the conversation instead of an outage.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from guardrail_sdk import (
    ActionBlocked,
    AgentContext,
    ApprovalRequired,
    GuardrailUnavailable,
    set_context,
)

from demo_agent.llm import LLMProvider, ToolCall, Turn
from demo_agent.tools import TOOL_REGISTRY, TOOL_SCHEMAS

SYSTEM_PROMPT = """You are an operations assistant for Acme Corp with access to real
systems. Use the provided tools to carry out what the user asks.

Facts you need:
- The internal email domain is acme-corp.com. Anything else is external.
- When deleting records, pass `count` if you know how many rows are affected.

Some actions are governed by policy and may be refused or held for human review. When
that happens you will receive a tool result explaining which policy applied. Do not retry
the same action. Tell the user plainly what was refused, name the policy, and suggest a
workable alternative."""


@dataclass
class ToolOutcome:
    """What happened to one tool call, for the transcript and for tests."""

    tool: str
    arguments: dict[str, Any]
    status: str  # executed | blocked | pending_approval | guardrail_unavailable | error
    detail: str
    rule_ids: list[str] = field(default_factory=list)
    decision_id: str | None = None
    audit_seq: int | None = None


@dataclass
class AgentRun:
    """The complete record of one task."""

    task: str
    session_id: str
    final_message: str
    outcomes: list[ToolOutcome] = field(default_factory=list)
    turns: int = 0

    @property
    def blocked(self) -> list[ToolOutcome]:
        return [o for o in self.outcomes if o.status == "blocked"]

    @property
    def executed(self) -> list[ToolOutcome]:
        return [o for o in self.outcomes if o.status == "executed"]

    @property
    def pending(self) -> list[ToolOutcome]:
        return [o for o in self.outcomes if o.status == "pending_approval"]


class Agent:
    """A governed tool-calling agent."""

    def __init__(
        self,
        llm: LLMProvider,
        *,
        agent_id: str = "ops-assistant",
        max_turns: int = 6,
        dry_run: bool = False,
    ) -> None:
        self.llm = llm
        self.agent_id = agent_id
        self.max_turns = max_turns
        self.dry_run = dry_run

    def run(self, task: str, *, session_id: str | None = None) -> AgentRun:
        """Work a task to completion, or until the turn budget is spent."""
        session = session_id or f"sess-{uuid.uuid4().hex[:12]}"

        # Every decorated tool reads this, so identity travels with the task rather than
        # being threaded through every call signature.
        set_context(
            AgentContext(
                agent_id=self.agent_id,
                session_id=session,
                principal={"type": "agent", "id": self.agent_id},
                context={"environment": "production"},
                dry_run=self.dry_run,
            )
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]
        outcomes: list[ToolOutcome] = []

        for turn_number in range(1, self.max_turns + 1):
            reply = self.llm.complete(messages, tools=TOOL_SCHEMAS)

            if not reply.wants_tools:
                return AgentRun(
                    task=task,
                    session_id=session,
                    final_message=reply.content.strip(),
                    outcomes=outcomes,
                    turns=turn_number,
                )

            messages.append(self._assistant_message(reply))

            for call in reply.tool_calls:
                outcome = self._invoke(call)
                outcomes.append(outcome)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": outcome.detail,
                    }
                )

        return AgentRun(
            task=task,
            session_id=session,
            final_message=(
                "I stopped after the maximum number of turns without reaching a "
                "conclusion. The actions attempted are listed above."
            ),
            outcomes=outcomes,
            turns=self.max_turns,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _assistant_message(reply: Turn) -> dict[str, Any]:
        """Re-serialise the model's tool calls so the next turn sees its own request."""
        import json

        return {
            "role": "assistant",
            "content": reply.content,
            "tool_calls": [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                }
                for c in reply.tool_calls
            ],
        }

    def _invoke(self, call: ToolCall) -> ToolOutcome:
        """Run one tool call through the guardrail.

        Every guardrail outcome is converted into a *tool result string*. The model reads
        it on the next turn and can respond to the refusal, which is the whole reason a
        block does not raise out of this loop.
        """
        func = TOOL_REGISTRY.get(call.name)
        if func is None:
            return ToolOutcome(
                tool=call.name,
                arguments=call.arguments,
                status="error",
                detail=f"No such tool: {call.name}. Available: {', '.join(TOOL_REGISTRY)}.",
            )

        policy_name = getattr(func, "guardrail_tool_name", call.name)

        try:
            result = func(**call.arguments)
        except ActionBlocked as blocked:
            return ToolOutcome(
                tool=policy_name,
                arguments=call.arguments,
                status="blocked",
                detail=(
                    f"REFUSED BY POLICY. {blocked.decision.explain()} "
                    "The action was not performed. Do not retry it."
                ),
                rule_ids=blocked.rule_ids,
                decision_id=blocked.decision.decision_id,
                audit_seq=blocked.decision.audit_seq,
            )
        except ApprovalRequired as pending:
            return ToolOutcome(
                tool=policy_name,
                arguments=call.arguments,
                status="pending_approval",
                detail=(
                    f"HELD FOR HUMAN REVIEW. {pending.decision.explain()} "
                    f"Approval request {pending.decision_id} is outstanding; the action "
                    "has not been performed yet. Do not retry it."
                ),
                rule_ids=pending.decision.rule_ids,
                decision_id=pending.decision_id,
                audit_seq=pending.decision.audit_seq,
            )
        except GuardrailUnavailable as unavailable:
            return ToolOutcome(
                tool=policy_name,
                arguments=call.arguments,
                status="guardrail_unavailable",
                detail=(
                    f"NOT PERFORMED. {unavailable}. The governance service could not "
                    "authorise this action, so it was refused rather than risked."
                ),
            )
        except TypeError as exc:
            # The model supplied arguments that do not fit the tool. Recoverable: the
            # message goes back so it can correct itself next turn.
            return ToolOutcome(
                tool=policy_name,
                arguments=call.arguments,
                status="error",
                detail=f"Invalid arguments for {call.name}: {exc}",
            )

        decision = getattr(func, "last_decision", None)
        note = ""
        if decision is not None and decision.decision == "log_and_allow":
            note = " (permitted, and recorded for review)"

        return ToolOutcome(
            tool=policy_name,
            arguments=call.arguments,
            status="executed",
            detail=f"OK: {result}{note}",
            rule_ids=decision.rule_ids if decision else [],
            decision_id=decision.decision_id if decision else None,
            audit_seq=decision.audit_seq if decision else None,
        )
