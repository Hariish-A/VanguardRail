"""The vocabulary the engine reasons about.

Everything the engine needs travels in the `ActionEnvelope`. It never reaches out to
fetch more context mid-evaluation -- that is what keeps a decision fast, deterministic,
and reproducible months later against the policy version that was in force at the time.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from guardrail_core.effects import Effect

ToolName = Annotated[str, Field(min_length=1, max_length=200)]


class Principal(BaseModel):
    """Who is attempting the action.

    `on_behalf_of` matters for governance: an agent acting for a specific user is a
    different risk profile from one acting autonomously, and auditors ask which it was.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["agent", "user", "service"] = "agent"
    id: str = Field(min_length=1, max_length=200)
    on_behalf_of: str | None = Field(default=None, max_length=200)


class ActionEnvelope(BaseModel):
    """One tool call an agent intends to make, normalized for evaluation.

    This is the request body of `POST /v1/evaluate`, and it is what gets written to the
    audit log. Nothing is dropped between the two, so the record explains exactly what
    was decided on.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(default="default", min_length=1, max_length=100)
    agent_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)

    tool: ToolName
    """The tool about to be invoked, e.g. `db.delete_records`."""

    arguments: dict[str, Any] = Field(default_factory=dict)
    """The arguments the agent produced. Matched against via `args.*` paths."""

    principal: Principal | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    """Ambient facts the caller supplies, e.g. `{"environment": "production"}`."""

    dry_run: bool = False
    """When true the caller will not execute the tool regardless of the decision.

    The engine still evaluates and still writes an audit record, flagged as a dry run and
    excluded from enforcement metrics -- which is what makes shadow testing meaningful.
    """

    idempotency_key: str | None = Field(default=None, max_length=200)
    """Lets a retried request return the original decision instead of creating a second
    audit record for one logical action."""


class RuleMatch(BaseModel):
    """A rule that fired, retained so the audit log explains *why*, not just *what*."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    effect: Effect
    severity: str
    message: str | None = None


class EvaluationResult(BaseModel):
    """The engine's verdict. Pure output of (envelope, bundle) -- no I/O involved."""

    model_config = ConfigDict(extra="forbid")

    effect: Effect
    matched_rules: list[RuleMatch] = Field(default_factory=list)
    """*Every* matching rule, not only the winner.

    A reviewer needs to know an action tripped four rules even if one dominated the
    outcome, and it is what makes policy debugging possible.
    """

    message: str | None = None
    """Explanation returned to the agent, from the winning rule."""

    bundle_id: str
    bundle_version: int
    """Pins the decision to the exact policy that produced it, so a past decision can be
    reproduced rather than re-litigated."""

    unknown_paths: list[str] = Field(default_factory=list)
    """Paths an extractor could not resolve.

    Surfaced rather than hidden: repeated UNKNOWNs mean a policy is being applied more
    conservatively than its author intended, and that is worth seeing.
    """

    @property
    def allows_execution(self) -> bool:
        """Whether the caller may dispatch the tool call right now."""
        return self.effect in (Effect.ALLOW, Effect.LOG_AND_ALLOW)
