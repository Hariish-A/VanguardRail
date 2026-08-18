"""Liveness, readiness, and build-identity endpoints.

`/healthz` and `/readyz` are deliberately distinct. Liveness answers "is this
process running"; readiness answers "can it actually serve traffic". Conflating
them is a common way to get a load balancer to route requests into a process that
is up but cannot reach its database.
"""

from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field

from guardrail_service.config import Settings, get_settings

router = APIRouter(tags=["health"])

_PROCESS_START = time.monotonic()


class HealthResponse(BaseModel):
    """Liveness payload."""

    status: Literal["ok"] = "ok"
    version: str = Field(description="Build identifier — the git SHA in deployed stages.")
    stage: str
    uptime_seconds: float


class DependencyStatus(BaseModel):
    """The state of one thing the service needs in order to serve traffic."""

    name: str
    ready: bool
    detail: str


class ReadinessResponse(BaseModel):
    """Readiness payload — enumerates dependencies rather than collapsing to a bool."""

    ready: bool
    version: str
    stage: str
    dependencies: list[DependencyStatus]


def _check_dependencies(settings: Settings) -> list[DependencyStatus]:
    """Report on everything required to serve a policy decision.

    M0 has no data-plane dependencies yet, so this reports the configuration state
    honestly rather than hard-coding success. M1 extends it with real DynamoDB
    reachability checks and an "active policy bundle is loaded" assertion.
    """
    tables = {
        "audit_table": settings.audit_table_name,
        "decisions_table": settings.decisions_table_name,
        "policies_table": settings.policies_table_name,
    }
    return [
        DependencyStatus(
            name=name,
            # Not yet provisioned in M0 — absence is expected, not a failure.
            ready=True,
            detail=f"configured as {value!r}" if value else "not provisioned until M1",
        )
        for name, value in tables.items()
    ]


@router.get("/healthz", response_model=HealthResponse, summary="Liveness probe")
def healthz() -> HealthResponse:
    """Return 200 whenever the process can execute code.

    Intentionally free of I/O: a liveness probe that touches a database will report
    the process as dead during a transient dependency outage and get it restarted,
    which makes the outage worse.
    """
    settings = get_settings()
    return HealthResponse(
        version=settings.version,
        stage=settings.stage,
        uptime_seconds=round(time.monotonic() - _PROCESS_START, 3),
    )


@router.get("/readyz", response_model=ReadinessResponse, summary="Readiness probe")
def readyz(response: Response) -> ReadinessResponse:
    """Return 200 only when every dependency needed to serve a decision is usable."""
    settings = get_settings()
    dependencies = _check_dependencies(settings)
    ready = all(dep.ready for dep in dependencies)

    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        ready=ready,
        version=settings.version,
        stage=settings.stage,
        dependencies=dependencies,
    )


class VersionResponse(BaseModel):
    """Build identity, so a deployed URL can be traced back to a commit."""

    version: str
    stage: str
    service: str


@router.get("/version", response_model=VersionResponse, summary="Build identity")
def version() -> VersionResponse:
    """Return the build identifier of the running code."""
    settings = get_settings()
    return VersionResponse(
        version=settings.version,
        stage=settings.stage,
        service=settings.service_name,
    )
