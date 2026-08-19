"""Human-in-the-loop endpoints.

    GET  /v1/decisions?status=pending   the review queue
    GET  /v1/decisions/{id}             poll one decision (the SDK waits on this)
    POST /v1/decisions/{id}/resolve     approve or deny

Every resolution is **appended to the audit chain** as its own record. The chain then
answers "who approved this, when, and what did they say" -- not merely "it was approved".
An approval that leaves no trace is indistinguishable from no policy at all.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, Field

from guardrail_service.auth import AuthenticatedCaller, require_api_key
from guardrail_service.dependencies import get_audit_repository, get_decision_repository
from guardrail_service.observability import logger
from guardrail_service.storage.audit import AuditRecord
from guardrail_service.storage.decisions import (
    DecisionAlreadyResolved,
    DecisionNotFound,
    PendingDecision,
)

router = APIRouter(prefix="/v1/decisions", tags=["decisions"])

DecisionId = Annotated[str, Path(min_length=1, max_length=200)]


class DecisionView(BaseModel):
    """What a reviewer (or a waiting agent) sees."""

    decision_id: str
    status: Literal["pending", "approved", "denied", "expired"]
    allows_execution: bool
    """Whether the agent may proceed now. Explicit so an SDK never has to infer it --
    a client that gets `expired` wrong in either direction is a security incident."""

    tool: str
    arguments: dict[str, Any]
    agent_id: str
    session_id: str
    matched_rules: list[dict[str, Any]] = Field(default_factory=list)
    message: str | None = None
    created_at: str
    expires_at: int
    seconds_remaining: int
    on_timeout: Literal["deny", "allow"]
    reviewers: list[str] = Field(default_factory=list)
    audit_seq: int = 0
    resolved_at: str | None = None
    reviewer: str | None = None
    reason: str | None = None

    @staticmethod
    def of(decision: PendingDecision) -> DecisionView:
        return DecisionView(
            decision_id=decision.decision_id,
            status=decision.effective_status,
            allows_execution=decision.allows_execution,
            tool=decision.tool,
            arguments=decision.arguments,
            agent_id=decision.agent_id,
            session_id=decision.session_id,
            matched_rules=decision.matched_rules,
            message=decision.message,
            created_at=decision.created_at,
            expires_at=decision.expires_at,
            seconds_remaining=decision.seconds_remaining,
            on_timeout=decision.on_timeout,
            reviewers=decision.reviewers,
            audit_seq=decision.audit_seq,
            resolved_at=decision.resolved_at,
            reviewer=decision.reviewer,
            reason=decision.reason,
        )


class DecisionQueue(BaseModel):
    decisions: list[DecisionView]
    count: int
    tenant_id: str


class ResolveRequest(BaseModel):
    """A reviewer's judgement."""

    approve: bool
    reason: str = Field(default="", max_length=1000)
    """Why. Recorded in the audit chain, because "who approved this" is only half the
    question an auditor asks."""

    reviewer: str | None = Field(default=None, max_length=200)
    """Defaults to the authenticated caller. Supplied only when a console acts on behalf
    of a named human it authenticated itself."""


@router.get(
    "",
    response_model=DecisionQueue,
    summary="List decisions awaiting review",
    dependencies=[Depends(require_api_key)],
)
async def list_decisions(
    caller: Annotated[AuthenticatedCaller, Depends(require_api_key)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> DecisionQueue:
    """The review queue, oldest first.

    Scoped to the caller's tenant from the API key, never a query parameter, so one
    tenant cannot read another's pending actions.
    """
    pending = get_decision_repository().list_pending(caller.tenant_id, limit=limit)
    return DecisionQueue(
        decisions=[DecisionView.of(d) for d in pending],
        count=len(pending),
        tenant_id=caller.tenant_id,
    )


@router.get(
    "/{decision_id}",
    response_model=DecisionView,
    summary="Poll one decision",
    dependencies=[Depends(require_api_key)],
)
async def get_decision(
    decision_id: DecisionId,
    caller: Annotated[AuthenticatedCaller, Depends(require_api_key)],
) -> DecisionView:
    """The endpoint a waiting SDK polls until the status changes."""
    try:
        decision = get_decision_repository().get(caller.tenant_id, decision_id)
    except DecisionNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No decision {decision_id} for this tenant.",
        ) from None

    return DecisionView.of(decision)


@router.post(
    "/{decision_id}/resolve",
    response_model=DecisionView,
    summary="Approve or deny a held action",
    dependencies=[Depends(require_api_key)],
)
async def resolve_decision(
    decision_id: DecisionId,
    body: ResolveRequest,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(require_api_key)],
) -> DecisionView:
    """Record a human judgement, then append it to the audit chain.

    Returns 409 if another reviewer resolved it first, or if the review window closed
    between the queue being rendered and the button being clicked -- both are things a
    reviewer needs told plainly rather than having their click silently do nothing.
    """
    reviewer = body.reviewer or caller.name or caller.key_id

    try:
        resolved = get_decision_repository().resolve(
            caller.tenant_id,
            decision_id,
            approve=body.approve,
            reviewer=reviewer,
            reason=body.reason,
        )
    except DecisionNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No decision {decision_id} for this tenant.",
        ) from None
    except DecisionAlreadyResolved as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    _record_resolution(resolved, request_id=request.headers.get("x-request-id", "unknown"))

    logger.info(
        "decision_resolved",
        extra={
            "decision_id": decision_id,
            "status": resolved.status,
            "reviewer": reviewer,
            "tool": resolved.tool,
        },
    )

    return DecisionView.of(resolved)


def _record_resolution(decision: PendingDecision, *, request_id: str) -> None:
    """Append the human judgement to the tamper-evident chain.

    Written as a normal chained record, so a later approval cannot be edited out without
    breaking verification -- the same guarantee the original decision has.

    A failure here is logged rather than raised. The judgement is already stored and the
    reviewer's click succeeded; turning an audit-append failure into a 500 would make
    them click again and risk a confusing double-resolution.
    """
    from guardrail_core.effects import Effect
    from guardrail_core.models import ActionEnvelope, EvaluationResult, RuleMatch

    envelope = ActionEnvelope(
        tenant_id=decision.tenant_id,
        agent_id=decision.agent_id,
        session_id=decision.session_id,
        tool=decision.tool,
        arguments=decision.arguments,
        context={
            "hitl_resolution": decision.status,
            "reviewer": decision.reviewer or "",
            "reason": decision.reason or "",
            "resolved_at": decision.resolved_at or "",
            "original_decision_id": decision.decision_id,
            "original_audit_seq": decision.audit_seq,
        },
    )

    result = EvaluationResult(
        effect=Effect.ALLOW if decision.allows_execution else Effect.BLOCK,
        matched_rules=[
            RuleMatch(
                rule_id=r.get("rule_id", "unknown"),
                effect=Effect.REQUIRE_HITL,
                severity=r.get("severity", "medium"),
            )
            for r in decision.matched_rules
        ],
        message=(
            f"Human review: {decision.status} by {decision.reviewer}"
            + (f" -- {decision.reason}" if decision.reason else "")
        ),
        bundle_id="hitl",
        bundle_version=0,
    )

    def build(seq: int, prev_hash: str) -> AuditRecord:
        return AuditRecord.build(
            tenant_id=decision.tenant_id,
            seq=seq,
            prev_hash=prev_hash,
            envelope=envelope,
            result=result,
            facts={"derived": {}},
            decision_id=f"{decision.decision_id}:resolution",
            latency_ms=0.0,
            request_id=request_id,
        )

    try:
        get_audit_repository().append(decision.tenant_id, build)
    except Exception:
        logger.exception(
            "hitl_resolution_audit_failed",
            extra={"decision_id": decision.decision_id},
        )
