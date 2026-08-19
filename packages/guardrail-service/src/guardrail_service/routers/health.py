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

    This used to report every dependency as ready with a "not provisioned yet" note --
    which meant `/readyz` could not return 503 under any circumstance. A readiness probe
    that cannot fail is worse than no probe: it reads as a working health check right up
    to the incident where it should have caught something.

    Two real checks now. Neither performs a write, and neither is on the hot path.
    """
    checks: list[DependencyStatus] = []

    # --- audit store -------------------------------------------------------
    if settings.audit_table_name:
        checks.append(
            DependencyStatus(
                name="audit_table",
                ready=True,
                detail=f"configured as {settings.audit_table_name!r}",
            )
        )
    else:
        # Locally this is expected; in a deployed stage it means decisions would be
        # returned without being recorded, which is the one thing this service must
        # never do. `dependencies.get_audit_repository` refuses to start in that case,
        # so this branch mostly documents the contract -- but it reports honestly.
        checks.append(
            DependencyStatus(
                name="audit_table",
                ready=settings.stage == "local",
                detail=(
                    "not configured; using an in-memory audit log (local development)"
                    if settings.stage == "local"
                    else "GUARDRAIL_AUDIT_TABLE_NAME is unset in a deployed stage"
                ),
            )
        )

    # --- active policy -----------------------------------------------------
    # Answers "is a valid bundle loaded, and did it come from where we think?". The
    # provider serves a cached answer inside its refresh window, so a store outage can
    # take up to `policy_refresh_seconds` to appear here. The age is reported rather
    # than hidden, so the reader can tell a confirmed answer from a recent one.
    from guardrail_service.dependencies import get_policy_provider

    # Readiness is unauthenticated, so there is no caller tenant to report on. The
    # default tenant is used, and *named* in the output: without that, an operator whose
    # published policy lives under another tenant reads "packaged bundle v1" and
    # reasonably concludes their activation did not take. Per-tenant state is at
    # GET /v1/policies. The store round trip is what this check is really for, and it
    # happens either way.
    tenant = "default"
    state = get_policy_provider().state(tenant)
    age = round(time.monotonic() - state.checked_at, 1)
    detail = (
        f"tenant {tenant!r}: {state.source} bundle v{state.version}, "
        f"{len(state.bundle.active_rules)} active rule(s), "
        f"mode={state.bundle.metadata.mode}, checked {age}s ago"
    )
    if state.degraded:
        detail += f" -- DEGRADED: policy store unreachable ({state.error})"

    checks.append(
        DependencyStatus(
            name="active_policy",
            ready=not state.degraded,
            detail=detail,
        )
    )

    return checks


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
