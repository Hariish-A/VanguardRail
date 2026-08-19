"""Dropping Guardrail into agent frameworks that already exist.

Three integration points, one enforcement rule. Each is small on purpose: the value is
not in the code, it is in the claim that governing an existing agent is a few lines
rather than a rewrite.

    LangChain            GuardrailCallbackHandler  -> callbacks=[handler]
    OpenAI tool loop     GuardedToolDispatcher     -> wraps your dispatch function
    MCP                  see `mcp.py`

## The rule all three obey

**Evaluate before dispatch, and refuse by returning a message the model can read.**

Raising an exception into a framework's tool loop usually aborts the run, which turns a
policy refusal into an outage. Returning a refusal *as the tool's result* keeps the agent
alive and lets it explain itself and choose something else -- which is what makes a
guardrail usable rather than merely obstructive. Every adapter here does that.

## Where LangChain's callback interface falls short, stated honestly

`BaseCallbackHandler.on_tool_start` is a **notification**, not an interception point:
LangChain does not offer a documented way for a callback to veto the tool it is about to
run. So the callback handler alone gives **detection, not enforcement** -- it records
every tool call and can raise, but raising aborts the chain.

Rather than ship that and call it governance, the handler is paired with
`guard_langchain_tool`, which wraps a tool's own callable so the check happens where it
can actually stop something. The callback handler remains useful for audit correlation
across a chain, and its docstring says exactly what it is. A component that looks like
enforcement but only observes is worse than no component.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from guardrail_sdk.client import GuardrailClient
from guardrail_sdk.exceptions import ActionBlocked, GuardrailUnavailable
from guardrail_sdk.models import Decision


def refusal_text(decision: Decision) -> str:
    """The sentence handed back to the model in place of a tool result.

    Names the rule, and says plainly that nothing happened. Both matter: without the
    rule the agent cannot tell the user what to do next, and without "was not performed"
    models frequently narrate the action as though it succeeded.
    """
    rules = ", ".join(decision.rule_ids)
    base = decision.message or f"The action was {decision.decision}."
    suffix = f" (policy: {rules})" if rules else ""
    return f"REFUSED BY POLICY. {base}{suffix} The action was not performed. Do not retry it."


def pending_text(decision: Decision) -> str:
    """What the model is told while a human decides."""
    rules = ", ".join(decision.rule_ids)
    base = decision.message or "This action needs human approval."
    suffix = f" (policy: {rules})" if rules else ""
    return (
        f"HELD FOR HUMAN REVIEW. {base}{suffix} The action was not performed and is "
        f"awaiting approval (decision {decision.decision_id}). Tell the user it is "
        "pending rather than retrying."
    )


# ---------------------------------------------------------------------------
# Framework-agnostic core
# ---------------------------------------------------------------------------


class GuardedToolDispatcher:
    """Wraps a tool-dispatch function so policy runs first.

    This is the OpenAI-compatible integration, and it is deliberately not
    OpenAI-specific: any loop that ends in "given a tool name and arguments, run it" can
    be wrapped, which covers Ollama, Groq, vLLM, and anything else speaking the same
    function-calling shape.

        dispatch = GuardedToolDispatcher(client, my_dispatch, agent_id="support-bot")
        result = dispatch("db.delete_records", {"table": "users", "count": 500})
        # -> the refusal string; my_dispatch was never called

    The wrapped function is not called at all when policy refuses. That ordering is the
    entire product.
    """

    def __init__(
        self,
        client: GuardrailClient,
        dispatch: Callable[[str, dict[str, Any]], Any],
        *,
        agent_id: str = "openai-agent",
        session_id: str | None = None,
        on_pending: str = "block",
        wait_timeout: float = 900.0,
        context: dict[str, Any] | None = None,
        principal: dict[str, Any] | None = None,
        tool_prefix: str = "",
    ) -> None:
        self.client = client
        self.dispatch = dispatch
        self.agent_id = agent_id
        self.session_id = session_id or f"session-{uuid.uuid4().hex[:12]}"
        self.on_pending = on_pending
        self.wait_timeout = wait_timeout
        self.context = context or {}
        self.principal = principal
        self.tool_prefix = tool_prefix
        self.decisions: list[Decision] = []
        """Every decision this dispatcher made, for assertions and demos."""

    def __call__(self, name: str, arguments: dict[str, Any]) -> Any:
        return self.call(name, arguments)

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        """Evaluate, then dispatch or refuse."""
        tool = f"{self.tool_prefix}{name}" if self.tool_prefix else name

        try:
            decision = self.client.evaluate(
                tool=tool,
                arguments=arguments,
                agent_id=self.agent_id,
                session_id=self.session_id,
                principal=self.principal,
                context=self.context,
                idempotency_key=str(uuid.uuid4()),
            )
        except GuardrailUnavailable as exc:
            # Fail closed. The client raises only when it is configured to, so honouring
            # it here rather than swallowing it is what keeps that setting meaningful.
            return (
                f"REFUSED: the guardrail could not be reached, so the action was not "
                f"performed. ({exc})"
            )

        self.decisions.append(decision)

        if decision.decision == "block":
            return refusal_text(decision)

        if decision.decision == "require_hitl":
            if self.on_pending != "wait":
                return pending_text(decision)

            final = self.client.wait_for_decision(decision.decision_id, timeout=self.wait_timeout)
            if not final.allows_execution:
                reviewer = f" by {final.reviewer}" if final.reviewer else ""
                reason = f": {final.reason}" if final.reason else ""
                return (
                    f"REFUSED: human review {final.status}{reviewer}{reason}. "
                    "The action was not performed."
                )

        return self.dispatch(name, arguments)

    # ------------------------------------------------------------------
    @property
    def blocked(self) -> list[Decision]:
        return [d for d in self.decisions if d.decision == "block"]

    @property
    def held(self) -> list[Decision]:
        return [d for d in self.decisions if d.decision == "require_hitl"]


def guard_openai_tool_calls(
    client: GuardrailClient,
    tool_calls: list[Any],
    dispatch: Callable[[str, dict[str, Any]], Any],
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Run a whole batch of OpenAI-style `tool_calls` under policy.

    Returns messages ready to append to the conversation, refusals included, so the model
    sees a normal tool result either way and can reason about the denial.

    Handles both the object form the SDKs return and the plain-dict form that comes off
    the wire, because agents in the wild carry both.
    """
    import json

    guarded = GuardedToolDispatcher(client, dispatch, **kwargs)
    messages: list[dict[str, Any]] = []

    for call in tool_calls:
        call_id, name, raw_arguments = _unpack_tool_call(call)

        try:
            arguments = (
                json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            )
        except json.JSONDecodeError:
            # A model can emit malformed JSON. Surfacing it as a tool result lets the
            # model correct itself; raising would end the run over a recoverable slip.
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": f"ERROR: arguments were not valid JSON: {raw_arguments!r}",
                }
            )
            continue

        result = guarded.call(name, arguments if isinstance(arguments, dict) else {})
        messages.append({"role": "tool", "tool_call_id": call_id, "content": str(result)})

    return messages


def _unpack_tool_call(call: Any) -> tuple[str, str, Any]:
    """Read a tool call in either the object or dict shape."""
    if isinstance(call, dict):
        function = call.get("function", {})
        return (
            str(call.get("id", "")),
            str(function.get("name", "")),
            function.get("arguments", "{}"),
        )
    return (
        str(getattr(call, "id", "")),
        str(getattr(getattr(call, "function", None), "name", "")),
        getattr(getattr(call, "function", None), "arguments", "{}"),
    )


# ---------------------------------------------------------------------------
# LangChain
# ---------------------------------------------------------------------------


def guard_langchain_tool(
    client: GuardrailClient,
    tool: Any,
    *,
    agent_id: str = "langchain-agent",
    session_id: str | None = None,
    context: dict[str, Any] | None = None,
    tool_name: str | None = None,
) -> Any:
    """Wrap a LangChain tool so policy is enforced **before** its function runs.

    This is the real enforcement point for LangChain, because callbacks cannot veto.
    Returns the same tool object with its callable replaced, so it drops into an existing
    agent with no other change:

        tools = [guard_langchain_tool(client, t) for t in tools]

    A refusal is *returned*, not raised. LangChain treats a raised exception in a tool as
    a run failure, and an agent that dies on a policy denial is worse than one that
    explains it.
    """
    name = tool_name or getattr(tool, "name", None) or tool.__class__.__name__
    session = session_id or f"lc-{uuid.uuid4().hex[:12]}"

    original = getattr(tool, "func", None)
    if original is None:
        raise TypeError(
            f"{name} has no `func` to wrap. Structured or async LangChain tools need "
            "their own wrapper; wrapping the wrong attribute would look governed while "
            "the original callable stayed reachable."
        )

    def guarded(*args: Any, **kwargs: Any) -> Any:
        arguments = dict(kwargs)
        if args:
            # Positional args carry no names at this layer. Recording them positionally
            # is honest; inventing names would make policies match on a fiction.
            arguments["args"] = list(args)

        try:
            decision = client.evaluate(
                tool=name,
                arguments=arguments,
                agent_id=agent_id,
                session_id=session,
                context=context or {},
                idempotency_key=str(uuid.uuid4()),
            )
        except GuardrailUnavailable as exc:
            return f"REFUSED: the guardrail could not be reached ({exc}); nothing was done."

        if decision.decision == "block":
            return refusal_text(decision)
        if decision.decision == "require_hitl":
            return pending_text(decision)

        return original(*args, **kwargs)

    tool.func = guarded
    return tool


class GuardrailCallbackHandler:
    """LangChain callback that **records** tool activity. It does not enforce.

    Read that again before relying on it. `on_tool_start` is a notification; LangChain
    provides no supported way for a callback to stop the tool it is announcing. So this
    class gives correlation and audit across a chain -- useful, and honestly labelled --
    while `guard_langchain_tool` is what actually prevents anything.

    Set `raise_on_block=True` and it will raise `ActionBlocked` from `on_tool_start`,
    which in practice aborts the chain. That is a blunt instrument offered for cases
    where aborting is genuinely preferable to proceeding; it is off by default because a
    dead agent is usually a worse outcome than a refused tool call.

    Subclasses `BaseCallbackHandler` at runtime when LangChain is installed, so the SDK
    never takes a hard dependency on it.
    """

    def __init__(
        self,
        client: GuardrailClient,
        *,
        agent_id: str = "langchain-agent",
        session_id: str | None = None,
        raise_on_block: bool = False,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.client = client
        self.agent_id = agent_id
        self.session_id = session_id or f"lc-{uuid.uuid4().hex[:12]}"
        self.raise_on_block = raise_on_block
        self.context = context or {}
        self.decisions: list[Decision] = []

    # LangChain calls this with the tool's serialized definition and its raw input.
    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        import json

        name = str(serialized.get("name", "unknown-tool"))
        try:
            parsed = json.loads(input_str)
            arguments = parsed if isinstance(parsed, dict) else {"input": parsed}
        except (json.JSONDecodeError, TypeError):
            arguments = {"input": input_str}

        try:
            decision = self.client.evaluate(
                tool=name,
                arguments=arguments,
                agent_id=self.agent_id,
                session_id=self.session_id,
                context={**self.context, "framework": "langchain"},
                idempotency_key=str(uuid.uuid4()),
            )
        except GuardrailUnavailable:
            if self.raise_on_block:
                raise
            return

        self.decisions.append(decision)

        if decision.decision == "block" and self.raise_on_block:
            raise ActionBlocked(decision, name)

    @property
    def blocked(self) -> list[Decision]:
        return [d for d in self.decisions if d.decision == "block"]


def make_langchain_handler(client: GuardrailClient, **kwargs: Any) -> Any:
    """Build a handler that really is a `BaseCallbackHandler` subclass.

    LangChain checks the type of things passed as callbacks, so duck typing is not
    enough. Built dynamically to keep LangChain an optional dependency: the SDK is
    installed by agent teams who should not inherit a framework they do not use.
    """
    try:
        from langchain_core.callbacks import BaseCallbackHandler
    except ImportError as exc:  # pragma: no cover - exercised only without langchain
        raise ImportError(
            "langchain-core is not installed. `guard_langchain_tool` needs no LangChain "
            "import and enforces properly -- prefer it unless you specifically need "
            "chain-wide callbacks."
        ) from exc

    bases = (GuardrailCallbackHandler, BaseCallbackHandler)
    handler_class = type("GuardrailLangChainHandler", bases, {})
    return handler_class(client, **kwargs)
