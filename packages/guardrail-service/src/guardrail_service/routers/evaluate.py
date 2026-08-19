"""`POST /v1/evaluate` -- the hot path.

An agent's SDK calls this before dispatching any tool call. The response says whether the
call may proceed, and every call is recorded whatever the answer.

Two ordering decisions matter here:

* **Evaluate, then audit, then respond.** The audit write happens before the decision is
  returned, so a caller can never act on a decision that was not recorded. If the write
  fails the request fails -- permitting an unrecorded action is the exact gap this
  system exists to close.
* **The engine never blocks on I/O.** Policy evaluation is pure and in-process, so a
  slow or unavailable dependency cannot delay the decision itself, only its persistence.
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from guardrail_core.effects import Effect
from guardrail_core.engine import build_facts, evaluate, winning_rule
from guardrail_core.models import ActionEnvelope, EvaluationResult, RuleMatch
from pydantic import BaseModel, Field

from guardrail_service.auth import AuthenticatedCaller, rate_limited_caller, require_api_key
from guardrail_service.config import get_settings
from guardrail_service.dependencies import (
    get_audit_repository,
    get_decision_repository,
    get_policy_provider,
)
from guardrail_service.observability import logger
from guardrail_service.storage.audit import AuditRecord, AuditWriteError
from guardrail_service.storage.decisions import DEFAULT_TIMEOUT_SECONDS, build_pending

router = APIRouter(prefix="/v1", tags=["evaluate"])


class HitlInstructions(BaseModel):
    """What the caller should do while a human decides."""

    decision_id: str
    timeout_seconds: int
    on_timeout: Literal["deny", "allow"]
    poll_url: str
    """Where to poll for resolution. Wired up in M3; the field exists now so SDK
    clients written against M1 keep working unchanged."""


class EvaluateResponse(BaseModel):
    """The verdict on one tool call."""

    decision: Literal["allow", "log_and_allow", "require_hitl", "block"]
    allowed: bool
    """Convenience for callers: true when the tool may be dispatched right now.

    Deliberately explicit rather than left for the SDK to derive -- a client that gets
    `log_and_allow` wrong in either direction is a security or availability incident.
    """

    matched_rules: list[RuleMatch]
    message: str | None = None
    decision_id: str
    audit_seq: int
    audit_hash: str
    bundle_id: str
    bundle_version: int
    unknown_paths: list[str] = Field(default_factory=list)
    dry_run: bool
    hitl: HitlInstructions | None = None
    latency_ms: float


@router.post(
    "/evaluate",
    response_model=EvaluateResponse,
    summary="Evaluate a tool call against policy before it executes",
    dependencies=[Depends(require_api_key)],
)
async def evaluate_action(
    envelope: ActionEnvelope,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(rate_limited_caller)],
) -> EvaluateResponse:
    """Decide whether a tool call may proceed."""
    started = time.perf_counter()

    # The caller's API key determines the tenant, not the request body. Otherwise any
    # authenticated caller could write into another tenant's audit chain by editing a
    # field -- a trivial cross-tenant forgery.
    envelope = envelope.model_copy(update={"tenant_id": caller.tenant_id})

    # A retried request must not produce a second decision or a second audit record
    # for one logical action. Networks drop responses; a fail-closed SDK retries; without
    # this the audit log would show the same delete attempted twice.
    if envelope.idempotency_key:
        cached = get_audit_repository().find_idempotent(
            envelope.tenant_id, envelope.idempotency_key
        )
        if cached is not None:
            logger.info(
                "idempotent_replay",
                extra={"idempotency_key": envelope.idempotency_key, "tool": envelope.tool},
            )
            return EvaluateResponse.model_validate(cached)

    # The active bundle, which may have been published and activated since this
    # container started. The provider caches it and re-checks on a timer, so a rollback
    # reaches every warm container within `policy_refresh_seconds` and no redeploy.
    bundle = get_policy_provider().get(envelope.tenant_id)
    result = evaluate(envelope, bundle)
    facts = build_facts(envelope)

    # A shadow bundle evaluates and records but never restrains the caller, so a policy
    # can be trialled against live traffic before it is enforced.
    effective = result.effect
    if bundle.is_shadow and effective in (Effect.BLOCK, Effect.REQUIRE_HITL):
        effective = Effect.LOG_AND_ALLOW

    decision_id = str(uuid.uuid4())
    request_id = request.headers.get("x-request-id", "unknown")
    latency_ms = (time.perf_counter() - started) * 1000

    record = _persist(
        envelope=envelope,
        result=result,
        facts=facts,
        decision_id=decision_id,
        request_id=request_id,
        latency_ms=latency_ms,
    )

    _emit_metrics(result, envelope, latency_ms)

    hitl = None
    if effective is Effect.REQUIRE_HITL:
        rule = winning_rule(result, bundle)
        options = rule.hitl_options if rule else None
        timeout_seconds = options.timeout_seconds if options else DEFAULT_TIMEOUT_SECONDS
        on_timeout = options.on_timeout if options else "deny"

        # The pending record is created before the response is returned, so the
        # decision_id the agent is told to poll always exists. Handing back an id that
        # is not yet queryable would make a fast SDK poll 404 on its first attempt.
        get_decision_repository().create(
            build_pending(
                decision_id=decision_id,
                tenant_id=envelope.tenant_id,
                tool=envelope.tool,
                arguments=envelope.arguments,
                agent_id=envelope.agent_id,
                session_id=envelope.session_id,
                matched_rules=[
                    {"rule_id": m.rule_id, "severity": m.severity, "effect": m.effect.wire_name}
                    for m in result.matched_rules
                ],
                message=result.message,
                audit_seq=record.seq,
                timeout_seconds=timeout_seconds,
                on_timeout=on_timeout,
                reviewers=list(options.reviewers) if options else [],
            )
        )

        hitl = HitlInstructions(
            decision_id=decision_id,
            timeout_seconds=timeout_seconds,
            on_timeout=on_timeout,
            poll_url=f"/v1/decisions/{decision_id}",
        )

    allowed = effective in (Effect.ALLOW, Effect.LOG_AND_ALLOW)

    logger.info(
        "evaluated",
        extra={
            "tool": envelope.tool,
            "effect": effective.wire_name,
            "matched_rules": [m.rule_id for m in result.matched_rules],
            "dry_run": envelope.dry_run,
            "audit_seq": record.seq,
            "decision_id": decision_id,
            "latency_ms": round(latency_ms, 3),
        },
    )

    response = EvaluateResponse(
        decision=effective.wire_name,
        allowed=allowed,
        matched_rules=result.matched_rules,
        message=result.message,
        decision_id=decision_id,
        audit_seq=record.seq,
        audit_hash=record.hash,
        bundle_id=result.bundle_id,
        bundle_version=result.bundle_version,
        unknown_paths=result.unknown_paths,
        dry_run=envelope.dry_run,
        hitl=hitl,
        latency_ms=round(latency_ms, 3),
    )

    if envelope.idempotency_key:
        # Stored after the audit write, so a replay can only ever return a decision that
        # was genuinely recorded. Storage failures are swallowed inside the repository:
        # the decision already stands, and losing idempotency on one request is far less
        # serious than rejecting a correctly evaluated action.
        get_audit_repository().store_idempotent(
            envelope.tenant_id, envelope.idempotency_key, response.model_dump(mode="json")
        )

    return response


def _persist(
    *,
    envelope: ActionEnvelope,
    result: EvaluationResult,
    facts: dict[str, Any],
    decision_id: str,
    request_id: str,
    latency_ms: float,
) -> AuditRecord:
    """Write the audit record, failing the request if it cannot be written.

    A 503 is the honest status: the service is temporarily unable to govern safely, and
    a fail-closed SDK will block the tool call. Returning a decision we could not record
    would be worse than returning none.
    """
    repository = get_audit_repository()

    def build(seq: int, prev_hash: str) -> AuditRecord:
        return AuditRecord.build(
            tenant_id=envelope.tenant_id,
            seq=seq,
            prev_hash=prev_hash,
            envelope=envelope,
            result=result,
            facts=facts,
            decision_id=decision_id,
            latency_ms=latency_ms,
            request_id=request_id,
        )

    try:
        return repository.append(envelope.tenant_id, build)
    except AuditWriteError as exc:
        logger.error("audit_write_failed", extra={"error": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The decision could not be recorded, so no decision is returned. "
                "A fail-closed client should block the action."
            ),
        ) from exc


def _emit_metrics(result: EvaluationResult, envelope: ActionEnvelope, latency_ms: float) -> None:
    """Publish the two metrics this endpoint owns.

    `single_metric` rather than the shared `metrics` object, because dimensions added to
    the shared instance persist for the life of the process. Lambda reuses warm
    containers across invocations, so a dimension set on one request would leak onto the
    next -- attributing a later allow to an earlier block. Each call here emits its own
    isolated EMF blob instead.

    Stays inside the ten-custom-metric CloudWatch budget: outcome is a *dimension* of one
    metric rather than four separate metrics, and everything finer-grained (per rule, per
    agent) is a structured log field queried through Logs Insights.
    """
    from aws_lambda_powertools.metrics import MetricUnit, single_metric

    settings = get_settings()

    with single_metric(
        name="dry_run.decisions" if envelope.dry_run else "decisions",
        unit=MetricUnit.Count,
        value=1,
        namespace=settings.service_name,
    ) as metric:
        metric.add_dimension(name="outcome", value=result.effect.wire_name)

    with single_metric(
        name="evaluate.latency_ms",
        unit=MetricUnit.Milliseconds,
        value=latency_ms,
        namespace=settings.service_name,
    ) as metric:
        metric.add_dimension(name="stage", value=settings.stage)
