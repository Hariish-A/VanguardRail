"""`/v1/policies` -- publishing, activating, and rolling back policy.

The lifecycle is deliberately three separate steps, because collapsing them is how
policy changes go wrong:

    validate  ->  does this parse and mean what I think?      (changes nothing)
    publish   ->  store it as an immutable numbered version    (changes nothing)
    activate  ->  make it the policy agents are judged by      (changes everything)

Publishing is safe, so it can happen in CI on every merge. Activating is the deliberate
act, and it is the only one that alters behaviour. Rolling back is `activate` pointing at
an older number -- there is no separate rollback code path, so there is no separate
rollback bug, and a rollback can only ever land on a bundle that was reviewed and stored.

**Reads are open to any authenticated caller; writes need `require_policy_admin`.** An
agent that could publish its own policy would not be governed by one. See `auth.py`.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from guardrail_core.operators import PolicyError
from pydantic import BaseModel, ConfigDict, Field

from guardrail_service.auth import AuthenticatedCaller, require_api_key, require_policy_admin
from guardrail_service.config import get_settings
from guardrail_service.dependencies import get_policy_provider, get_policy_repository
from guardrail_service.observability import logger
from guardrail_service.storage.policies import (
    PolicyNotFound,
    PolicyRepository,
    PublishedBundle,
    content_hash,
    semantic_hash,
    validate_document,
)

router = APIRouter(prefix="/v1/policies", tags=["policies"])


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class BundleSubmission(BaseModel):
    """A bundle offered for validation or publication.

    Accepts either parsed JSON or raw YAML text. YAML is what people actually author and
    review in pull requests, and making the API refuse the format the policy is written
    in would push everyone into a conversion step that can itself introduce a mistake.
    """

    model_config = ConfigDict(extra="forbid")

    bundle: dict[str, Any] | None = None
    yaml: str | None = None
    description: str = Field(default="", max_length=500)
    """Why this version exists. Stored with it, so the history explains itself."""

    def document(self) -> dict[str, Any]:
        if (self.bundle is None) == (self.yaml is None):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Provide exactly one of `bundle` (JSON) or `yaml` (text).",
            )

        if self.bundle is not None:
            return self.bundle

        import yaml as yaml_module

        try:
            parsed = yaml_module.safe_load(self.yaml or "")
        except yaml_module.YAMLError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Not valid YAML: {exc}",
            ) from exc

        if not isinstance(parsed, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"A policy bundle must be a mapping, got {type(parsed).__name__}.",
            )
        return parsed


class ValidationResponse(BaseModel):
    """Whether a bundle would be accepted, without storing anything."""

    valid: bool
    detail: str
    bundle_id: str = ""
    rule_count: int = 0
    active_rule_count: int = 0
    mode: str = ""
    content_hash: str = ""
    matches_active: bool = False
    """True when this expresses the same policy already in force.

    Compared semantically -- key order, omitted defaults, and the version number are not
    differences, because none of them change a single decision.

    Worth returning: "publish v9" after an edit that changed nothing is a strong signal
    the file being edited is not the file being deployed.
    """


class VersionSummary(BaseModel):
    """One published version, without its body."""

    version: int
    published_at: str
    published_by: str
    description: str
    content_hash: str
    rule_count: int
    mode: str
    is_active: bool


class PolicyListResponse(BaseModel):
    bundle_id: str
    tenant_id: str
    active_version: int | None
    active_source: str
    """`published` when a stored version is in force, `packaged` when the deployment is
    still running the bundle baked into its artifact."""

    degraded: bool = False
    versions: list[VersionSummary] = Field(default_factory=list)


class ActiveBundleResponse(BaseModel):
    """The bundle currently governing this tenant, and where it came from."""

    bundle_id: str
    version: int
    source: str
    degraded: bool
    rule_count: int
    mode: str
    document: dict[str, Any]


class PublishResponse(BaseModel):
    bundle_id: str
    version: int
    content_hash: str
    published_at: str
    published_by: str
    activated: bool
    detail: str


class ActivationResponse(BaseModel):
    bundle_id: str
    active_version: int
    previous_version: int | None
    activated_at: str
    activated_by: str
    direction: str
    """`rollback`, `rollforward`, or `unchanged`. Named explicitly because a rollback
    during an incident should be identifiable in the audit trail without arithmetic."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repository() -> PolicyRepository:
    repository = get_policy_repository()
    if repository is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "No policy store is configured, so this deployment serves only the "
                "bundle packaged with its build. Set GUARDRAIL_AUDIT_TABLE_NAME."
            ),
        )
    return repository


def _summary(published: PublishedBundle, active_version: int | None) -> VersionSummary:
    rules = published.document.get("rules", [])
    metadata = published.document.get("metadata", {})
    return VersionSummary(
        version=published.version,
        published_at=published.published_at,
        published_by=published.published_by,
        description=published.description,
        content_hash=published.content_hash,
        rule_count=len(rules) if isinstance(rules, list) else 0,
        mode=str(metadata.get("mode", "enforce")) if isinstance(metadata, dict) else "enforce",
        is_active=published.version == active_version,
    )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=PolicyListResponse,
    summary="List published policy versions and which one is active",
    dependencies=[Depends(require_api_key)],
)
async def list_policies(
    caller: Annotated[AuthenticatedCaller, Depends(require_api_key)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> PolicyListResponse:
    """Newest version first."""
    settings = get_settings()
    bundle_id = settings.policy_bundle_id
    state = get_policy_provider().state(caller.tenant_id)

    repository = get_policy_repository()
    if repository is None:
        return PolicyListResponse(
            bundle_id=bundle_id,
            tenant_id=caller.tenant_id,
            active_version=state.version,
            active_source=state.source,
            degraded=state.degraded,
            versions=[],
        )

    pointer = repository.get_active(caller.tenant_id, bundle_id)
    active_version = pointer.active_version if pointer else None
    versions = repository.list_versions(caller.tenant_id, bundle_id, limit=limit)

    return PolicyListResponse(
        bundle_id=bundle_id,
        tenant_id=caller.tenant_id,
        active_version=active_version,
        active_source=state.source,
        degraded=state.degraded,
        versions=[_summary(v, active_version) for v in versions],
    )


@router.get(
    "/active",
    response_model=ActiveBundleResponse,
    summary="The bundle currently in force",
    dependencies=[Depends(require_api_key)],
)
async def get_active_policy(
    caller: Annotated[AuthenticatedCaller, Depends(require_api_key)],
) -> ActiveBundleResponse:
    """What agents are actually being judged by right now.

    Read from the provider rather than the store, so it reports the bundle this
    container is *serving* -- including the packaged fallback during a store outage. A
    view that read the store instead would show the intended policy during exactly the
    incident where the difference matters.
    """
    settings = get_settings()
    state = get_policy_provider().state(caller.tenant_id)

    return ActiveBundleResponse(
        bundle_id=settings.policy_bundle_id,
        version=state.version,
        source=state.source,
        degraded=state.degraded,
        rule_count=len(state.bundle.rules),
        mode=state.bundle.metadata.mode,
        document=state.bundle.model_dump(mode="json"),
    )


@router.get(
    "/versions/{version}",
    summary="Fetch one published version",
    dependencies=[Depends(require_api_key)],
)
async def get_version(
    version: int,
    caller: Annotated[AuthenticatedCaller, Depends(require_api_key)],
) -> dict[str, Any]:
    """The stored document, exactly as published."""
    try:
        published = _repository().get_version(
            caller.tenant_id, get_settings().policy_bundle_id, version
        )
    except PolicyNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return {
        "version": published.version,
        "bundle_id": published.bundle_id,
        "content_hash": published.content_hash,
        "published_at": published.published_at,
        "published_by": published.published_by,
        "description": published.description,
        "document": published.document,
    }


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


@router.post(
    "/validate",
    response_model=ValidationResponse,
    summary="Check a bundle without storing it",
    dependencies=[Depends(require_api_key)],
)
async def validate_policy(
    caller: Annotated[AuthenticatedCaller, Depends(require_api_key)],
    submission: Annotated[BundleSubmission, Body()],
) -> ValidationResponse:
    """Validate only. Open to any authenticated caller, because it changes nothing.

    Returns 200 with `valid: false` for a bad bundle rather than a 4xx. This is a
    linting endpoint, and a CI step that has to distinguish "the request failed" from
    "the policy is wrong" by parsing an error body is a CI step that gets it wrong.
    """
    document = submission.document()

    try:
        bundle = validate_document(document)
    except PolicyError as exc:
        return ValidationResponse(valid=False, detail=str(exc))

    from guardrail_core.policy import load_bundle

    parsed = load_bundle(bundle)
    state = get_policy_provider().state(caller.tenant_id)

    return ValidationResponse(
        valid=True,
        detail=f"{len(parsed.active_rules)} active rule(s) parsed successfully.",
        bundle_id=parsed.metadata.bundle_id,
        rule_count=len(parsed.rules),
        active_rule_count=len(parsed.active_rules),
        mode=parsed.metadata.mode,
        content_hash=content_hash(document),
        # Semantic, not byte-for-byte: the active bundle has been through the parser and
        # carries a store-assigned version, so a raw document comparison would report
        # "different" every time and the field would silently mean nothing.
        matches_active=semantic_hash(parsed) == semantic_hash(state.bundle),
    )


@router.post(
    "",
    response_model=PublishResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Publish a new immutable policy version",
    dependencies=[Depends(require_policy_admin)],
)
async def publish_policy(
    caller: Annotated[AuthenticatedCaller, Depends(require_policy_admin)],
    submission: Annotated[BundleSubmission, Body()],
    activate: Annotated[
        bool,
        Query(
            description="Activate immediately. Off by default -- publishing is safe, "
            "activating is the deliberate act."
        ),
    ] = False,
) -> PublishResponse:
    """Store a bundle as the next version. Does not change behaviour unless `activate`."""
    document = submission.document()
    settings = get_settings()
    repository = _repository()

    try:
        published = repository.publish(
            caller.tenant_id,
            settings.policy_bundle_id,
            document,
            published_by=f"{caller.key_id}:{caller.name}",
            description=submission.description,
        )
    except PolicyError as exc:
        # A bundle that does not parse never reaches the store. Activating one later
        # would break evaluation for every agent at once.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    logger.info(
        "policy_published",
        extra={
            "tenant_id": caller.tenant_id,
            "bundle_id": published.bundle_id,
            "version": published.version,
            "content_hash": published.content_hash,
            "published_by": published.published_by,
            "activate": activate,
        },
    )

    detail = f"Published version {published.version}. Not in force until activated."
    if activate:
        repository.activate(
            caller.tenant_id,
            settings.policy_bundle_id,
            published.version,
            activated_by=f"{caller.key_id}:{caller.name}",
        )
        get_policy_provider().invalidate(caller.tenant_id)
        detail = f"Published and activated version {published.version}."

    return PublishResponse(
        bundle_id=published.bundle_id,
        version=published.version,
        content_hash=published.content_hash,
        published_at=published.published_at,
        published_by=published.published_by,
        activated=activate,
        detail=detail,
    )


@router.post(
    "/versions/{version}/activate",
    response_model=ActivationResponse,
    summary="Make a published version the policy in force (this is also rollback)",
    dependencies=[Depends(require_policy_admin)],
)
async def activate_version(
    version: int,
    caller: Annotated[AuthenticatedCaller, Depends(require_policy_admin)],
) -> ActivationResponse:
    """Point the active marker at an existing version.

    Rolling back is this call with a lower number. Deliberately not a separate endpoint:
    a distinct rollback path is code that runs only during incidents, which is the worst
    possible test coverage profile for the operation you most need to work.

    Warm containers pick the change up within `policy_refresh_seconds`; this container's
    cache is invalidated immediately, so the operator who activated it sees the effect on
    their next request rather than up to half a minute later.
    """
    settings = get_settings()
    repository = _repository()

    previous = repository.get_active(caller.tenant_id, settings.policy_bundle_id)
    previous_version = previous.active_version if previous else None

    try:
        pointer = repository.activate(
            caller.tenant_id,
            settings.policy_bundle_id,
            version,
            activated_by=f"{caller.key_id}:{caller.name}",
        )
    except PolicyNotFound as exc:
        # Activating a version that was never published would leave every agent
        # evaluating against nothing.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    get_policy_provider().invalidate(caller.tenant_id)

    if previous_version is None or previous_version == version:
        direction = "unchanged" if previous_version == version else "rollforward"
    else:
        direction = "rollback" if version < previous_version else "rollforward"

    logger.info(
        "policy_activated",
        extra={
            "tenant_id": caller.tenant_id,
            "bundle_id": settings.policy_bundle_id,
            "from_version": previous_version,
            "to_version": version,
            "direction": direction,
            "activated_by": pointer.activated_by,
        },
    )

    return ActivationResponse(
        bundle_id=settings.policy_bundle_id,
        active_version=pointer.active_version,
        previous_version=previous_version,
        activated_at=pointer.activated_at,
        activated_by=pointer.activated_by,
        direction=direction,
    )
