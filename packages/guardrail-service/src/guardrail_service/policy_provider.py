"""Serving the active policy, and reloading it without a redeploy.

The evaluation path needs a bundle on every request, and it must not pay a DynamoDB read
to get one. So the active bundle is cached per warm container and re-checked on a timer:
a small `GetItem` against the pointer item, and a full fetch only when the version
actually changed.

## Why a timer rather than a push

Lambda freezes a container between invocations, so a background poller or a subscription
would not run. The choice is between checking on the request path and not reloading at
all. A `refresh_seconds` window is therefore an explicit, tunable bound on how stale a
policy may be -- 30 seconds by default, which is fast enough that activating a rollback
during an incident feels immediate, and slow enough that the extra reads are negligible
against a 5 RCU table.

## Behaviour when the store is unreachable -- read this before changing it

The rule is **keep serving the last known good bundle, indefinitely**. A policy store
outage must not become an agent outage, and the previously active policy is a far better
answer than no policy at all.

There is exactly one ambiguous case: a cold start while the store is unreachable, where
there is no "last known good" to fall back on. Three options, and the trade-off is real:

1. *Fail the request.* Fail-closed clients then block every action -- the whole agent
   fleet halts because one DynamoDB call timed out.
2. *Allow everything.* Never. This is the failure mode the product exists to prevent.
3. *Serve the packaged bundle* -- the reviewed policy that shipped with this build.

Option 3 is chosen, with a caveat that must not be lost: if the published policy was
*stricter* than the packaged one, this is a silent weakening. So it is not silent. The
event is logged at error level, `/readyz` reports the degradation, and every decision
made this way carries the packaged bundle's id and version in its audit record -- so it
is distinguishable after the fact, forever.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from guardrail_core.policy import PolicyBundle

from guardrail_service.observability import logger
from guardrail_service.storage.policies import PolicyRepository

DEFAULT_REFRESH_SECONDS = 30.0

PACKAGED_SOURCE = "packaged"
"""The bundle baked into the deployment artifact. Always available, never stale."""

PUBLISHED_SOURCE = "published"
"""A bundle published through the API and marked active."""


@dataclass
class PolicyState:
    """What the provider is currently serving, and how it got there."""

    bundle: PolicyBundle
    version: int
    source: str
    checked_at: float
    degraded: bool = False
    """True when the store could not be reached and this is a fallback rather than a
    confirmed answer. Surfaced by `/readyz`, never merely logged."""

    error: str | None = None


class ActivePolicyProvider:
    """Per-tenant cache of the active bundle, refreshed on a timer."""

    def __init__(
        self,
        repository: PolicyRepository | None,
        fallback: PolicyBundle,
        *,
        bundle_id: str = "default",
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
        clock: Any = time.monotonic,
    ) -> None:
        self._repository = repository
        self._fallback = fallback
        self._bundle_id = bundle_id
        self._refresh_seconds = refresh_seconds
        self._clock = clock
        self._state: dict[str, PolicyState] = {}

    # ------------------------------------------------------------------
    def get(self, tenant_id: str = "default") -> PolicyBundle:
        """The bundle to evaluate this tenant's actions against."""
        return self.state(tenant_id).bundle

    def state(self, tenant_id: str = "default") -> PolicyState:
        """The bundle plus its provenance. Used by `/readyz` and the policies API."""
        cached = self._state.get(tenant_id)

        if cached is not None and (self._clock() - cached.checked_at) < self._refresh_seconds:
            return cached

        if self._repository is None:
            # No policy store configured at all -- local development. The packaged
            # bundle is the whole story, and there is nothing to be stale about.
            state = PolicyState(
                bundle=self._fallback,
                version=self._fallback.metadata.version,
                source=PACKAGED_SOURCE,
                checked_at=self._clock(),
            )
            self._state[tenant_id] = state
            return state

        try:
            state = self._refresh(tenant_id, cached)
        except Exception as exc:
            state = self._degrade(tenant_id, cached, exc)

        self._state[tenant_id] = state
        return state

    # ------------------------------------------------------------------
    def _refresh(self, tenant_id: str, cached: PolicyState | None) -> PolicyState:
        """Re-check the pointer, and fetch the bundle only if the version moved."""
        assert self._repository is not None  # narrowed by the caller
        pointer = self._repository.get_active(tenant_id, self._bundle_id)

        if pointer is None:
            # Nothing published yet. The packaged bundle is the active policy, which is
            # what makes a fresh deployment govern correctly before anyone touches the
            # policy API.
            if cached is not None and cached.source == PACKAGED_SOURCE and not cached.degraded:
                return PolicyState(
                    bundle=cached.bundle,
                    version=cached.version,
                    source=PACKAGED_SOURCE,
                    checked_at=self._clock(),
                )
            return PolicyState(
                bundle=self._fallback,
                version=self._fallback.metadata.version,
                source=PACKAGED_SOURCE,
                checked_at=self._clock(),
            )

        if (
            cached is not None
            and not cached.degraded
            and cached.source == PUBLISHED_SOURCE
            and cached.version == pointer.active_version
        ):
            # Unchanged. One small read, no parse, no allocation -- which is what keeps
            # the refresh affordable on the request path.
            return PolicyState(
                bundle=cached.bundle,
                version=cached.version,
                source=PUBLISHED_SOURCE,
                checked_at=self._clock(),
            )

        published = self._repository.get_version(tenant_id, self._bundle_id, pointer.active_version)
        bundle = published.bundle

        logger.info(
            "policy_reloaded",
            extra={
                "tenant_id": tenant_id,
                "bundle_id": self._bundle_id,
                "from_version": cached.version if cached else None,
                "to_version": published.version,
                "activated_by": pointer.activated_by,
                "active_rules": len(bundle.active_rules),
                "mode": bundle.metadata.mode,
            },
        )
        _emit_policy_version(published.version)

        return PolicyState(
            bundle=bundle,
            version=published.version,
            source=PUBLISHED_SOURCE,
            checked_at=self._clock(),
        )

    def _degrade(self, tenant_id: str, cached: PolicyState | None, exc: Exception) -> PolicyState:
        """Handle a store failure without failing the request.

        With a cached bundle, keep serving it: it is the policy that was genuinely
        active, and nothing about a failed read makes it wrong. Without one, fall back
        to the packaged bundle and say so loudly -- see the module docstring for why
        that beats both refusing every request and permitting every action.
        """
        if cached is not None:
            logger.warning(
                "policy_refresh_failed",
                extra={
                    "tenant_id": tenant_id,
                    "error": str(exc),
                    "serving_version": cached.version,
                    "serving_source": cached.source,
                    "note": "continuing on the last known good bundle",
                },
            )
            return PolicyState(
                bundle=cached.bundle,
                version=cached.version,
                source=cached.source,
                checked_at=self._clock(),
                degraded=True,
                error=str(exc),
            )

        logger.error(
            "policy_store_unreachable_at_cold_start",
            extra={
                "tenant_id": tenant_id,
                "error": str(exc),
                "note": (
                    "falling back to the packaged bundle; if the published policy was "
                    "stricter, this deployment is temporarily governing more loosely"
                ),
            },
        )
        return PolicyState(
            bundle=self._fallback,
            version=self._fallback.metadata.version,
            source=PACKAGED_SOURCE,
            checked_at=self._clock(),
            degraded=True,
            error=str(exc),
        )

    # ------------------------------------------------------------------
    def invalidate(self, tenant_id: str | None = None) -> None:
        """Drop the cache so the next request re-reads.

        Called after an activation through this same container, so the operator who
        clicked "activate" sees the effect immediately rather than up to
        `refresh_seconds` later. Other warm containers still converge on the timer --
        this is an optimisation for the caller, not the reload mechanism.
        """
        if tenant_id is None:
            self._state.clear()
        else:
            self._state.pop(tenant_id, None)


def _emit_policy_version(version: int) -> None:
    """Publish `policy.version`, one of the ten budgeted CloudWatch metrics.

    A gauge of which policy is live, so a dashboard shows a rollback landing rather than
    requiring someone to query for it. Emitted only on an actual change, so it costs one
    data point per activation instead of one per request.
    """
    try:
        from aws_lambda_powertools.metrics import MetricUnit, single_metric

        from guardrail_service.config import get_settings

        with single_metric(
            name="policy.version",
            unit=MetricUnit.Count,
            value=version,
            namespace=get_settings().service_name,
        ) as metric:
            metric.add_dimension(name="stage", value=get_settings().stage)
    except Exception as exc:
        logger.debug("policy_version_metric_failed", extra={"error": str(exc)})
