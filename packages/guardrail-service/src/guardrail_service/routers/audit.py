"""`GET /v1/audit` and `GET /v1/audit/verify`.

The audit log is the deliverable, not a by-product. It answers two different questions:

* *What happened?* -- `/v1/audit`, filterable by outcome.
* *Can I trust what it says?* -- `/v1/audit/verify`, which walks the hash chain.

The second is what separates an audit log from a log file. Anyone can append to a log;
the chain is what makes a silent edit detectable.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from guardrail_service.auth import AuthenticatedCaller, require_api_key
from guardrail_service.dependencies import get_audit_repository

router = APIRouter(prefix="/v1/audit", tags=["audit"])

Outcome = Literal["allow", "log_and_allow", "require_hitl", "block"]


class AuditEntry(BaseModel):
    """One audit record as returned by the API."""

    seq: int
    timestamp: str
    hash: str
    prev_hash: str
    decision_id: str
    tool: str
    effect: str
    matched_rules: list[dict[str, Any]] = Field(default_factory=list)
    agent_id: str
    session_id: str
    dry_run: bool
    message: str | None = None
    bundle_id: str
    bundle_version: int
    arguments: dict[str, Any] = Field(default_factory=dict)
    derived: dict[str, Any] = Field(default_factory=dict)
    unknown_paths: list[str] = Field(default_factory=list)
    latency_ms: float | None = None


class AuditListResponse(BaseModel):
    entries: list[AuditEntry]
    count: int
    tenant_id: str


class VerifyResponse(BaseModel):
    """Whether the chain is intact.

    `reason` distinguishes a sequence gap (a deleted record) from a broken link (a
    reordered or substituted one) from a content mismatch (an edited one) -- they imply
    different incidents.
    """

    chain_valid: bool
    records_checked: int
    tenant_id: str
    broken_at_seq: int | None = None
    reason: str | None = None


def _to_entry(record: Any) -> AuditEntry:
    payload = record.payload
    return AuditEntry(
        seq=record.seq,
        timestamp=record.timestamp,
        hash=record.hash,
        prev_hash=record.prev_hash,
        decision_id=payload.get("decision_id", ""),
        tool=payload.get("tool", ""),
        effect=payload.get("effect", ""),
        matched_rules=payload.get("matched_rules", []),
        agent_id=payload.get("agent_id", ""),
        session_id=payload.get("session_id", ""),
        dry_run=bool(payload.get("dry_run", False)),
        message=payload.get("message"),
        bundle_id=payload.get("bundle_id", ""),
        bundle_version=int(payload.get("bundle_version", 0)),
        arguments=payload.get("arguments", {}),
        derived=payload.get("derived", {}),
        unknown_paths=payload.get("unknown_paths", []),
        latency_ms=payload.get("latency_ms"),
    )


@router.get(
    "",
    response_model=AuditListResponse,
    summary="Query the audit log",
    dependencies=[Depends(require_api_key)],
)
async def list_audit(
    caller: Annotated[AuthenticatedCaller, Depends(require_api_key)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    effect: Annotated[Outcome | None, Query(description="Filter by outcome")] = None,
) -> AuditListResponse:
    """Most recent records first, scoped to the caller's tenant.

    Tenant comes from the API key rather than a query parameter, so one tenant cannot
    read another's log by editing a URL.
    """
    records = get_audit_repository().list_records(caller.tenant_id, limit=limit, effect=effect)
    return AuditListResponse(
        entries=[_to_entry(r) for r in records],
        count=len(records),
        tenant_id=caller.tenant_id,
    )


@router.get(
    "/verify",
    response_model=VerifyResponse,
    summary="Verify the audit hash chain end to end",
    dependencies=[Depends(require_api_key)],
)
async def verify_audit(
    caller: Annotated[AuthenticatedCaller, Depends(require_api_key)],
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
) -> VerifyResponse:
    """Recompute every hash and confirm each record links to its predecessor.

    An empty chain verifies as valid: nothing has been tampered with because nothing has
    been written.
    """
    verification = get_audit_repository().verify_chain(caller.tenant_id, limit=limit)
    return VerifyResponse(
        chain_valid=verification.chain_valid,
        records_checked=verification.records_checked,
        tenant_id=verification.tenant_id,
        broken_at_seq=verification.broken_at_seq,
        reason=verification.reason,
    )
