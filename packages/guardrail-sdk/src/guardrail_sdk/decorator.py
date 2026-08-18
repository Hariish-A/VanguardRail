"""`@governed_tool` -- the enforcement point.

The decorator runs the policy check **before** the wrapped function body. That ordering
is the whole product: detecting a bulk delete after it ran is forensics, refusing it
beforehand is a control.

    @governed_tool("db.delete_records")
    def delete_records(table: str, count: int) -> int:
        return db.execute(...)      # unreachable when policy says block

Agent identity comes from a context variable rather than a parameter on every call, so
adding governance to an existing tool is one line and no signature changes.
"""

from __future__ import annotations

import functools
import inspect
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, ParamSpec, TypeVar

from guardrail_sdk.client import GuardrailClient
from guardrail_sdk.exceptions import ActionBlocked, ApprovalRequired
from guardrail_sdk.models import Decision

P = ParamSpec("P")
R = TypeVar("R")


@dataclass
class AgentContext:
    """Who is acting, and under what conditions."""

    agent_id: str = "unknown-agent"
    session_id: str = "unknown-session"
    principal: dict[str, Any] | None = None
    context: dict[str, Any] | None = None
    dry_run: bool = False
    """When true no tool body executes, whatever policy says.

    Distinct from a `block`: the engine still evaluates and still records, so a dry run
    reports exactly what *would* have happened. That is what makes shadow-testing a
    policy change meaningful.
    """


_context: ContextVar[AgentContext] = ContextVar("guardrail_agent_context")
_client: ContextVar[GuardrailClient | None] = ContextVar("guardrail_client", default=None)


def get_context() -> AgentContext:
    try:
        return _context.get()
    except LookupError:
        return AgentContext()


def set_context(ctx: AgentContext) -> None:
    """Install the acting identity for this task/thread."""
    _context.set(ctx)


def set_client(client: GuardrailClient) -> None:
    """Install the process-wide client used by decorated tools."""
    _client.set(client)


def get_client() -> GuardrailClient | None:
    return _client.get()


def _bind_arguments(
    func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Normalise call arguments into the flat mapping policies are written against.

    Defaults are applied first, so a rule matching `args.count` still fires when the
    caller relied on the default rather than passing it -- otherwise a threshold could be
    bypassed simply by omitting the parameter.
    """
    signature = inspect.signature(func)
    bound = signature.bind_partial(*args, **kwargs)
    bound.apply_defaults()

    arguments = dict(bound.arguments)
    arguments.pop("self", None)
    arguments.pop("cls", None)

    # Flatten **kwargs so nested keys are addressable as args.<name>.
    for name, parameter in signature.parameters.items():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD and name in arguments:
            extra = arguments.pop(name)
            if isinstance(extra, dict):
                arguments.update(extra)

    safe: dict[str, Any] = _json_safe(arguments)
    return safe


def _json_safe(value: Any) -> Any:
    """Reduce arbitrary Python values to something the API can carry.

    Unrepresentable objects become their repr rather than raising: a tool taking a
    database handle must still be governable, and its other arguments are what the policy
    cares about.
    """
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def governed_tool(
    name: str,
    *,
    client: GuardrailClient | None = None,
    on_pending: str = "raise",
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Wrap a callable so policy is enforced before it runs.

    `on_pending` controls what a `require_hitl` decision does. `raise` surfaces
    ApprovalRequired so the agent can tell the user an approval is outstanding; `block`
    treats a pause as a refusal. Pausing and refusing mean different things to the person
    waiting, so they are kept distinguishable rather than collapsed.
    """

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            active = client or get_client()
            if active is None:
                raise RuntimeError(
                    f"{name} is decorated with @governed_tool but no GuardrailClient is "
                    "configured. Call set_client(...) during startup. Refusing to run "
                    "an ungoverned tool."
                )

            ctx = get_context()
            arguments = _bind_arguments(func, args, kwargs)

            decision = active.evaluate(
                tool=name,
                arguments=arguments,
                agent_id=ctx.agent_id,
                session_id=ctx.session_id,
                principal=ctx.principal,
                context=ctx.context,
                dry_run=ctx.dry_run,
                idempotency_key=str(uuid.uuid4()),
            )

            wrapper.last_decision = decision  # type: ignore[attr-defined]

            if decision.decision == "block":
                raise ActionBlocked(decision, name)

            if decision.decision == "require_hitl":
                if on_pending == "block":
                    raise ActionBlocked(decision, name)
                raise ApprovalRequired(decision, name)

            if ctx.dry_run:
                # Permitted, but a dry run never touches the real world. Returning None
                # rather than a fabricated value keeps the caller honest about what it
                # did and did not do.
                return None  # type: ignore[return-value]

            return func(*args, **kwargs)

        wrapper.guardrail_tool_name = name  # type: ignore[attr-defined]
        wrapper.last_decision = None  # type: ignore[attr-defined]
        return wrapper

    return decorate


def last_decision(func: Callable[..., Any]) -> Decision | None:
    """The most recent decision for a decorated tool. For tests and demos."""
    return getattr(func, "last_decision", None)
