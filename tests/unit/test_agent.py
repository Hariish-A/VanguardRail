"""Agent loop behaviour, with a scripted model.

A real LLM is deliberately not used here. These tests must assert exact behaviour, and a
model that picks a slightly different tool on a different day would make a genuine
governance regression indistinguishable from model variance. The live Qwen3 runs are the
integration proof; these are the specification.

The property that matters most: a refusal must reach the model as a **tool result**, not
as an exception. If a block crashes the agent, the person who asked for something gets a
stack trace instead of an explanation.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from demo_agent.agent import Agent
from demo_agent.llm import Turn
from demo_agent.tools import SIDE_EFFECTS, reset_side_effects
from guardrail_sdk import GuardrailClient, set_client


class ScriptedLLM:
    """Replays a fixed sequence of turns, and records what it was told."""

    def __init__(self, turns: list[Turn]) -> None:
        self._turns = list(turns)
        self.seen_messages: list[list[dict[str, Any]]] = []

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> Turn:
        self.seen_messages.append([dict(m) for m in messages])
        if not self._turns:
            return Turn(content="Nothing further.")
        return self._turns.pop(0)


def _tool_turn(name: str, arguments: dict[str, Any]) -> Turn:
    from demo_agent.llm import ToolCall

    return Turn(content="", tool_calls=[ToolCall(id="call_1", name=name, arguments=arguments)])


def _guardrail(decision: str) -> GuardrailClient:
    body = {
        "decision": decision,
        "allowed": decision in ("allow", "log_and_allow"),
        "matched_rules": [
            {"rule_id": "db-bulk-delete", "effect": decision, "severity": "critical"}
        ],
        "message": "Blocked: this would delete 500 records, above the limit of 100.",
        "decision_id": "d-42",
        "audit_seq": 99,
        "bundle_id": "default",
        "bundle_version": 1,
    }
    if decision == "require_hitl":
        body["hitl"] = {
            "decision_id": "d-42",
            "timeout_seconds": 900,
            "on_timeout": "deny",
            "poll_url": "/v1/decisions/d-42",
        }

    return GuardrailClient(
        base_url="https://guardrail.test",
        api_key="k",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    )


@pytest.fixture(autouse=True)
def _clean() -> None:
    reset_side_effects()


# ---------------------------------------------------------------------------


def test_a_blocked_call_produces_no_side_effect() -> None:
    set_client(_guardrail("block"))
    llm = ScriptedLLM(
        [
            _tool_turn("db_delete_records", {"table": "users", "count": 500}),
            Turn(content="I could not delete those records because policy blocked it."),
        ]
    )

    run = Agent(llm).run("delete all inactive users")  # type: ignore[arg-type]

    assert SIDE_EFFECTS == [], "the tool executed despite being blocked"
    assert len(run.blocked) == 1
    assert run.blocked[0].rule_ids == ["db-bulk-delete"]


def test_the_refusal_is_fed_back_to_the_model_as_a_tool_result() -> None:
    """This is what makes the guardrail part of the conversation rather than an outage."""
    set_client(_guardrail("block"))
    llm = ScriptedLLM(
        [
            _tool_turn("db_delete_records", {"table": "users", "count": 500}),
            Turn(content="Policy blocked that."),
        ]
    )

    Agent(llm).run("delete everything")  # type: ignore[arg-type]

    # The second turn's message list is what the model saw after the refusal.
    second_turn = llm.seen_messages[1]
    tool_results = [m for m in second_turn if m.get("role") == "tool"]

    assert tool_results, "the model was never told the call was refused"
    content = tool_results[-1]["content"]
    assert "REFUSED BY POLICY" in content
    assert "db-bulk-delete" in content, "the refusal must name the policy"
    assert "Do not retry" in content


def test_a_blocked_call_does_not_raise_out_of_the_loop() -> None:
    """If a block crashed the agent, the user would get a stack trace instead of an
    explanation."""
    set_client(_guardrail("block"))
    llm = ScriptedLLM(
        [
            _tool_turn("db_delete_records", {"table": "users", "count": 500}),
            Turn(content="Explained to the user."),
        ]
    )

    run = Agent(llm).run("delete everything")  # type: ignore[arg-type]

    assert run.final_message == "Explained to the user."


def test_a_pending_call_is_distinguished_from_a_block() -> None:
    """Pausing and refusing mean different things to the person waiting."""
    set_client(_guardrail("require_hitl"))
    llm = ScriptedLLM(
        [
            _tool_turn("email_send", {"to": "x@external.com", "subject": "hi"}),
            Turn(content="Awaiting approval."),
        ]
    )

    run = Agent(llm).run("email the vendor")  # type: ignore[arg-type]

    assert len(run.pending) == 1
    assert run.blocked == []
    assert SIDE_EFFECTS == []
    assert run.pending[0].decision_id == "d-42"

    content = [m for m in llm.seen_messages[1] if m.get("role") == "tool"][-1]["content"]
    assert "HELD FOR HUMAN REVIEW" in content
    assert "d-42" in content, "the approval id must reach the model so it can be quoted"


def test_an_allowed_call_executes_and_is_recorded() -> None:
    set_client(_guardrail("allow"))
    llm = ScriptedLLM(
        [
            _tool_turn("file_read", {"path": "/tmp/notes.txt"}),
            Turn(content="Here is the file."),
        ]
    )

    run = Agent(llm).run("read my notes")  # type: ignore[arg-type]

    assert len(SIDE_EFFECTS) == 1
    assert SIDE_EFFECTS[0].tool == "file.read"
    assert len(run.executed) == 1


def test_log_and_allow_executes_and_is_flagged_to_the_model() -> None:
    set_client(_guardrail("log_and_allow"))
    llm = ScriptedLLM(
        [
            _tool_turn("file_read", {"path": "/srv/confidential/x.pdf"}),
            Turn(content="Read it."),
        ]
    )

    Agent(llm).run("read the confidential file")  # type: ignore[arg-type]

    assert len(SIDE_EFFECTS) == 1
    content = [m for m in llm.seen_messages[1] if m.get("role") == "tool"][-1]["content"]
    assert "recorded for review" in content


def test_guardrail_outage_refuses_the_action_and_says_so() -> None:
    client = GuardrailClient(
        base_url="https://guardrail.test",
        api_key="k",
        max_retries=0,
        transport=httpx.MockTransport(lambda r: httpx.Response(503)),
    )
    set_client(client)
    llm = ScriptedLLM(
        [
            _tool_turn("db_delete_records", {"table": "users", "count": 1}),
            Turn(content="Could not verify authorisation."),
        ]
    )

    run = Agent(llm).run("delete one row")  # type: ignore[arg-type]

    assert SIDE_EFFECTS == []
    assert run.outcomes[0].status == "guardrail_unavailable"
    content = [m for m in llm.seen_messages[1] if m.get("role") == "tool"][-1]["content"]
    assert "NOT PERFORMED" in content


def test_an_unknown_tool_is_reported_without_crashing() -> None:
    set_client(_guardrail("allow"))
    llm = ScriptedLLM([_tool_turn("delete_the_internet", {}), Turn(content="No such tool.")])

    run = Agent(llm).run("do something impossible")  # type: ignore[arg-type]

    assert run.outcomes[0].status == "error"
    assert SIDE_EFFECTS == []


def test_bad_arguments_are_reported_so_the_model_can_correct_itself() -> None:
    set_client(_guardrail("allow"))
    llm = ScriptedLLM(
        [
            _tool_turn("file_read", {"wrong_parameter": "x"}),
            Turn(content="I used the wrong argument."),
        ]
    )

    run = Agent(llm).run("read something")  # type: ignore[arg-type]

    assert run.outcomes[0].status == "error"
    assert SIDE_EFFECTS == []


def test_dry_run_executes_nothing_even_when_policy_allows() -> None:
    set_client(_guardrail("allow"))
    llm = ScriptedLLM([_tool_turn("file_read", {"path": "/tmp/x"}), Turn(content="Simulated.")])

    Agent(llm, dry_run=True).run("read a file")  # type: ignore[arg-type]

    assert SIDE_EFFECTS == [], "a dry run must not touch the real world"


def test_a_plain_answer_ends_the_run_without_tools() -> None:
    set_client(_guardrail("allow"))
    llm = ScriptedLLM([Turn(content="Acme Corp was founded in 1998.")])

    run = Agent(llm).run("when was the company founded?")  # type: ignore[arg-type]

    assert run.outcomes == []
    assert run.turns == 1
    assert SIDE_EFFECTS == []


def test_the_turn_budget_is_enforced() -> None:
    """A model that keeps calling tools must not loop forever."""
    set_client(_guardrail("allow"))
    llm = ScriptedLLM([_tool_turn("file_read", {"path": f"/tmp/{i}"}) for i in range(20)])

    run = Agent(llm, max_turns=3).run("read everything")  # type: ignore[arg-type]

    assert run.turns == 3
    assert len(SIDE_EFFECTS) == 3
