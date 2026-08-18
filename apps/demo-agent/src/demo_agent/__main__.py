"""CLI for the governed agent.

    uv run python -m demo_agent "delete all inactive user accounts"

Reads GUARDRAIL_BASE_URL and GUARDRAIL_API_KEY for the control plane, and
GUARDRAIL_LLM_* for inference. Both are checked before the task starts, so a
misconfiguration is reported as such rather than as a timeout halfway through.
"""

from __future__ import annotations

import argparse
import os
import sys

from guardrail_sdk import GuardrailClient, set_client

from demo_agent.agent import Agent, AgentRun
from demo_agent.llm import LLMConfig, LLMProvider
from demo_agent.tools import SIDE_EFFECTS, reset_side_effects

STATUS_MARK = {
    "executed": "[EXECUTED]",
    "blocked": "[BLOCKED  ]",
    "pending_approval": "[REVIEW   ]",
    "guardrail_unavailable": "[REFUSED  ]",
    "error": "[ERROR    ]",
}


def _print_run(run: AgentRun) -> None:
    print("\n" + "=" * 78)
    print(f"TASK: {run.task}")
    print(f"session: {run.session_id}   turns: {run.turns}")
    print("=" * 78)

    if run.outcomes:
        print("\nTOOL CALLS THE MODEL ATTEMPTED\n")
        for outcome in run.outcomes:
            mark = STATUS_MARK.get(outcome.status, "[?        ]")
            print(f"  {mark} {outcome.tool}")
            print(f"             args: {outcome.arguments}")
            if outcome.rule_ids:
                print(f"             policy: {', '.join(outcome.rule_ids)}")
            if outcome.audit_seq:
                print(f"             audit seq: {outcome.audit_seq}")
            print(f"             {outcome.detail}")
            print()
    else:
        print("\n(the model made no tool calls)\n")

    print("-" * 78)
    print("WHAT ACTUALLY HAPPENED (side effects)\n")
    if SIDE_EFFECTS:
        for effect in SIDE_EFFECTS:
            print(f"  * {effect.detail}")
    else:
        print("  (nothing -- every attempted action was refused or held)")

    print("\n" + "-" * 78)
    print("AGENT'S REPLY\n")
    print(f"  {run.final_message or '(no closing message)'}")
    print("=" * 78 + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", nargs="?", help="what the agent should try to do")
    parser.add_argument("--agent-id", default="ops-assistant")
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--max-turns", type=int, default=6)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="evaluate and record every action without executing any of them",
    )
    parser.add_argument("--model", default=None, help="override the model name")
    parser.add_argument(
        "--fail-open",
        action="store_true",
        help="permit actions when the guardrail is unreachable (NOT recommended)",
    )
    args = parser.parse_args()

    if not args.task:
        parser.error('give the agent a task, e.g. "delete all inactive users"')

    base_url = os.environ.get("GUARDRAIL_BASE_URL", "")
    if not base_url:
        print(
            "error: GUARDRAIL_BASE_URL is not set. Point it at the deployed control "
            "plane, e.g.\n"
            "  export GUARDRAIL_BASE_URL=https://<id>.lambda-url.us-east-1.on.aws",
            file=sys.stderr,
        )
        return 2
    if not os.environ.get("GUARDRAIL_API_KEY"):
        print(
            "error: GUARDRAIL_API_KEY is not set. Mint one with\n"
            "  uv run python scripts/generate_api_key.py --tenant acme",
            file=sys.stderr,
        )
        return 2

    llm_config = LLMConfig()
    if args.model:
        llm_config.model = args.model

    client = GuardrailClient(fail_open=args.fail_open)
    # Installs the client every @governed_tool reads. Without this the decorator
    # refuses to run rather than executing an ungoverned tool -- correct, but it
    # would make the agent useless.
    set_client(client)

    print(f"guardrail: {base_url}")
    print(f"model:     {llm_config.model} at {llm_config.base_url}")
    if args.dry_run:
        print("mode:      DRY RUN -- decisions are recorded, nothing executes")
    if args.fail_open:
        print("mode:      FAIL OPEN -- actions proceed if the guardrail is unreachable")

    # Both dependencies are checked up front. Discovering mid-task that Ollama is not
    # running produces a far worse error than saying so now.
    if not client.health():
        print(f"\nerror: the guardrail at {base_url} is not responding.", file=sys.stderr)
        return 1

    with LLMProvider(llm_config) as llm:
        if not llm.available():
            print(
                f"\nerror: no model server at {llm_config.base_url}. Start it with `ollama serve`.",
                file=sys.stderr,
            )
            return 1

        reset_side_effects()
        agent = Agent(
            llm,
            agent_id=args.agent_id,
            max_turns=args.max_turns,
            dry_run=args.dry_run,
        )

        print("\nthinking...\n")
        run = agent.run(args.task, session_id=args.session_id)

    _print_run(run)
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
