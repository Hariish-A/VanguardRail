"""Wiring: the policy bundle and the audit repository.

Both are process-wide singletons built at cold start. Lambda reuses warm containers, so
this work is amortised across many invocations -- reloading and revalidating the policy
on every request would be the single largest cost in the evaluation path.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from guardrail_core.operators import PolicyError
from guardrail_core.policy import PolicyBundle, load_bundle_yaml

from guardrail_service.config import get_settings
from guardrail_service.observability import logger
from guardrail_service.storage.audit import (
    AuditRepository,
    DynamoDBAuditRepository,
    InMemoryAuditRepository,
)
from guardrail_service.storage.decisions import (
    DecisionRepository,
    DynamoDBDecisionRepository,
    InMemoryDecisionRepository,
)

# Packaged alongside the code, so the running Lambda always has a policy even if every
# external dependency is unreachable. M4 adds versioned bundles in DynamoDB with hot
# reload; this file remains the fallback.
_BUNDLED_POLICY = Path(__file__).parent / "policies" / "default.yaml"


@lru_cache(maxsize=1)
def get_bundle() -> PolicyBundle:
    """Load and validate the active policy bundle.

    A malformed bundle raises here, at cold start, rather than mid-evaluation. That is
    deliberate: the container fails to initialise and the deploy is visibly broken,
    instead of every request silently falling back to a default effect.
    """
    if not _BUNDLED_POLICY.is_file():
        raise PolicyError(f"no policy bundle found at {_BUNDLED_POLICY}")

    bundle = load_bundle_yaml(_BUNDLED_POLICY.read_text(encoding="utf-8"))

    logger.info(
        "policy_loaded",
        extra={
            "bundle_id": bundle.metadata.bundle_id,
            "bundle_version": bundle.metadata.version,
            "active_rules": len(bundle.active_rules),
            "mode": bundle.metadata.mode,
        },
    )
    return bundle


@lru_cache(maxsize=1)
def get_audit_repository() -> AuditRepository:
    """The audit store.

    Falls back to an in-memory repository only when no table is configured, which is the
    local-development case. In a deployed stage the table name is always set, so a
    missing table surfaces as an error rather than as silently discarded audit records.
    """
    settings = get_settings()

    if not settings.audit_table_name:
        if settings.stage != "local":
            raise RuntimeError(
                "GUARDRAIL_AUDIT_TABLE_NAME is unset in a deployed stage. Refusing to "
                "start with an in-memory audit log, which would discard every record."
            )
        logger.warning("audit_repository_in_memory", extra={"reason": "no table configured"})
        return InMemoryAuditRepository()

    return DynamoDBAuditRepository(settings.audit_table_name)


@lru_cache(maxsize=1)
def get_decision_repository() -> DecisionRepository:
    """The human-review decision store.

    Shares the audit table: a second table would need its own provisioned capacity, and
    15 of the account's free 25 WCU are already committed. Pending decisions live under a
    `DECISION#` sort key and reuse the outcome index as a sparse queue index.
    """
    settings = get_settings()

    if not settings.audit_table_name:
        if settings.stage != "local":
            raise RuntimeError(
                "GUARDRAIL_AUDIT_TABLE_NAME is unset in a deployed stage. Refusing to "
                "hold human-review decisions in memory, where a cold start would "
                "silently discard every pending approval."
            )
        return InMemoryDecisionRepository()

    return DynamoDBDecisionRepository(settings.audit_table_name)


def reset_caches() -> None:
    """Clear the singletons. For tests, and for policy reload in M4."""
    get_bundle.cache_clear()
    get_audit_repository.cache_clear()
    get_decision_repository.cache_clear()
