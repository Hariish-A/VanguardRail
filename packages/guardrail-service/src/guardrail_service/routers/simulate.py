"""`POST /v1/simulate` -- what would policy say, without anything happening.

Two questions this answers that `/v1/evaluate` cannot:

* *Would this candidate bundle change anything?* Evaluate an action against a version
  that is published but not active, or against a bundle that is not published at all.
* *What would the active policy do about an action nobody has taken?* Explore the policy
  without writing a decision into the evidence chain.

## Why simulation is not audited

`/v1/evaluate` writes an audit record for every call, on purpose: it is the record of
what an agent actually attempted. A simulation is not an attempt. Recording thousands of
what-ifs alongside real decisions would dilute the one log that is supposed to answer
"what did this agent do", and a reviewer would have to filter evidence from speculation
on every query.

The trade-off is honest and worth naming: simulations therefore leave no trace, so this
endpoint must not be usable to *discover* policy by an attacker who should not know it.
It is authenticated, tenant-scoped, and every call is still emitted as a structured log
line -- so the activity is visible in CloudWatch even though it is absent from the chain.

## Why it cannot cause the thing it is measuring

No audit write, no pending-decision record, no metrics against the enforcement counters,
and `dry_run` is forced true in the response. There is no code path from here to a state
change, which is what makes it safe to point change-impact analysis at production.
"""

from __future__ import annotations

import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, status
from guardrail_core.effects import Effect
from guardrail_core.engine import build_facts, evaluate
from guardrail_core.models import ActionEnvelope, RuleMatch
from guardrail_core.operators import PolicyError
from guardrail_core.policy import PolicyBundle, load_bundle
from pydantic import BaseModel, ConfigDict, Field

from guardrail_service.auth import AuthenticatedCaller, require_api_key
from guardrail_service.config import get_settings
from guardrail_service.dependencies import get_policy_provider, get_policy_repository
from guardrail_service.observability import logger
from guardrail_service.storage.policies import PolicyNotFound

router = APIRouter(prefix="/v1", tags=["simulate"])


class SimulateRequest(BaseModel):
    """One action, and optionally the policy to judge it by."""

    model_config = ConfigDict(extra="forbid")

    action: ActionEnvelope

    version: int | None = None
    """A published version to simulate against. Omit for the active policy."""

    bundle: dict[str, Any] | None = None
    """An unpublished candidate bundle, inline.

    This is what makes change-impact analysis possible *before* a policy is stored --
    reviewing a pull request should not require publishing the branch.
    """


class SimulateResponse(BaseModel):
    """What would have happened. Nothing did."""

    decision: Literal["allow", "log_and_allow", "require_hitl", "block"]
    allowed: bool
    matched_rules: list[RuleMatch]
    message: str | None = None
    bundle_id: str
    bundle_version: int
    bundle_source: str
    """`active`, `version`, or `candidate` -- so a report can never mistake a simulation
    against an inline draft for one against the live policy."""

    unknown_paths: list[str] = Field(default_factory=list)
    derived: dict[str, Any] = Field(default_factory=dict)
    """The facts the engine derived. The most useful field when a rule did not fire and
    the author cannot see why."""

    dry_run: Literal[True] = True
    simulated: Literal[True] = True
    latency_ms: float


def _resolve_bundle(request: SimulateRequest, tenant_id: str) -> tuple[PolicyBundle, str]:
    """Pick the bundle to evaluate against, and label where it came from."""
    if request.bundle is not None and request.version is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Provide a candidate `bundle` or a published `version`, not both.",
        )

    if request.bundle is not None:
        try:
            return load_bundle(request.bundle), "candidate"
        except PolicyError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc

    if request.version is not None:
        repository = get_policy_repository()
        if repository is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="No policy store is configured, so published versions cannot be read.",
            )
        try:
            published = repository.get_version(
                tenant_id, get_settings().policy_bundle_id, request.version
            )
        except PolicyNotFound as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return published.bundle, "version"

    return get_policy_provider().get(tenant_id), "active"


@router.post(
    "/simulate",
    response_model=SimulateResponse,
    summary="Evaluate an action against any bundle, with no side effects",
    dependencies=[Depends(require_api_key)],
)
async def simulate_action(
    request: Annotated[SimulateRequest, Body()],
    caller: Annotated[AuthenticatedCaller, Depends(require_api_key)],
) -> SimulateResponse:
    """Report the decision an action would receive. Writes nothing, holds nothing."""
    started = time.perf_counter()

    # Tenant comes from the key, exactly as on the enforcement path. Without this, a
    # caller could simulate against another tenant's published policy and read it back
    # rule by rule from the responses.
    envelope = request.action.model_copy(update={"tenant_id": caller.tenant_id})

    bundle, source = _resolve_bundle(request, caller.tenant_id)
    result = evaluate(envelope, bundle)
    facts = build_facts(envelope)

    # Shadow mode is applied here too. A simulation that ignored it would report a
    # `block` for a bundle whose whole purpose is to not block yet.
    effective = result.effect
    if bundle.is_shadow and effective in (Effect.BLOCK, Effect.REQUIRE_HITL):
        effective = Effect.LOG_AND_ALLOW

    latency_ms = (time.perf_counter() - started) * 1000

    # Logged but not audited -- see the module docstring. This is what keeps simulation
    # activity visible in CloudWatch without diluting the evidence chain.
    logger.info(
        "simulated",
        extra={
            "tool": envelope.tool,
            "effect": effective.wire_name,
            "matched_rules": [m.rule_id for m in result.matched_rules],
            "bundle_source": source,
            "bundle_version": result.bundle_version,
            "latency_ms": round(latency_ms, 3),
        },
    )

    return SimulateResponse(
        decision=effective.wire_name,
        allowed=effective in (Effect.ALLOW, Effect.LOG_AND_ALLOW),
        matched_rules=result.matched_rules,
        message=result.message,
        bundle_id=result.bundle_id,
        bundle_version=result.bundle_version,
        bundle_source=source,
        unknown_paths=result.unknown_paths,
        derived=_json_safe(facts.get("derived", {})),
        latency_ms=round(latency_ms, 3),
    )


def _json_safe(value: Any) -> Any:
    """Render UNKNOWN as a string so the response preserves the distinction between
    "could not be determined" and "absent". Collapsing the two here would hide the
    single most common reason a rule fired unexpectedly."""
    from guardrail_core.operators import UNKNOWN

    if value is UNKNOWN:
        return "UNKNOWN"
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
