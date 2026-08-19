"""API key authentication.

The Lambda Function URL is deliberately public (see `infra/stacks/service_stack.py`),
so authentication is the control that actually protects data. `/v1/evaluate` sees raw
tool-call arguments -- database filters, email bodies, file paths -- so it must never be
callable anonymously.

**Keys are never stored.** Only a SHA-256 hash is kept, in an SSM Parameter Store
SecureString (free; Secrets Manager charges $0.40 per secret per month). A leaked
parameter therefore discloses no usable credential.

**Comparison is constant-time.** `==` on secrets leaks length and prefix information
through timing, which is enough to recover a key given enough attempts.

M5 replaces the SSM document with a DynamoDB table for per-tenant key rotation and
revocation without redeploying. The `Principal` returned here is already tenant-scoped so
that change touches only this module.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, status

from guardrail_service.observability import logger

API_KEY_HEADER = "x-api-key"


@dataclass(frozen=True)
class AuthenticatedCaller:
    """Who is making the request, once their key has been verified."""

    key_id: str
    tenant_id: str
    name: str


def hash_key(raw_key: str) -> str:
    """Hash an API key for storage or comparison."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def _load_key_table() -> dict[str, AuthenticatedCaller]:
    """Load the hash -> caller mapping, once per warm Lambda container.

    Cached because this sits on the hot path of every tool call an agent makes; an SSM
    round trip per evaluation would dominate the latency budget. The cache dies with the
    container, so a rotated key takes effect within minutes without a redeploy.

    Source is `GUARDRAIL_API_KEYS_JSON` (injected by CDK from SSM):

        {"<sha256>": {"key_id": "...", "tenant_id": "...", "name": "..."}}
    """
    raw = os.environ.get("GUARDRAIL_API_KEYS_JSON", "").strip()
    if not raw:
        return {}

    try:
        parsed: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError:
        # Deliberately fail closed: an unparseable key table means no key can be
        # verified, so every authenticated route rejects rather than admits.
        logger.error("api_key_table_unparseable")
        return {}

    table: dict[str, AuthenticatedCaller] = {}
    for key_hash, meta in parsed.items():
        if not isinstance(meta, dict):
            continue
        table[key_hash] = AuthenticatedCaller(
            key_id=str(meta.get("key_id", "unknown")),
            tenant_id=str(meta.get("tenant_id", "default")),
            name=str(meta.get("name", "unnamed")),
        )
    return table


def reset_key_cache() -> None:
    """Clear the cached key table. For tests, and for key rotation in a warm container."""
    _load_key_table.cache_clear()


def _verify(raw_key: str) -> AuthenticatedCaller | None:
    """Constant-time lookup of a presented key."""
    presented = hash_key(raw_key)

    # Every entry is compared even after a match, so response time does not reveal
    # where in the table a key sits.
    matched: AuthenticatedCaller | None = None
    for stored_hash, caller in _load_key_table().items():
        if hmac.compare_digest(presented, stored_hash):
            matched = caller
    return matched


async def require_api_key(
    x_api_key: Annotated[str | None, Header(alias=API_KEY_HEADER)] = None,
) -> AuthenticatedCaller:
    """FastAPI dependency enforcing a valid API key.

    Registering this as a route dependency is also what satisfies
    `test_every_data_endpoint_is_authenticated`, which fails the build if any non-health
    route lacks one.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing {API_KEY_HEADER} header.",
            headers={"WWW-Authenticate": API_KEY_HEADER},
        )

    caller = _verify(x_api_key)
    if caller is None:
        # The key is never echoed back, not even truncated: error strings reach logs,
        # bug trackers, and screenshots.
        logger.warning("auth_rejected", extra={"reason": "unrecognised_api_key"})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
        )

    logger.append_keys(tenant_id=caller.tenant_id, key_id=caller.key_id)
    return caller


CallerDependency = Annotated[AuthenticatedCaller, Depends(require_api_key)]


POLICY_ADMIN_ENV = "GUARDRAIL_POLICY_ADMIN_KEY_IDS"


def _policy_admin_key_ids() -> frozenset[str]:
    """Key ids permitted to publish or activate policy.

    Read from the environment on every call rather than cached: this is not a hot path,
    and being able to revoke a policy-admin key by changing one environment variable
    without waiting for a container to recycle is worth more than the microseconds.
    """
    raw = os.environ.get(POLICY_ADMIN_ENV, "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


async def require_policy_admin(
    caller: Annotated[AuthenticatedCaller, Depends(require_api_key)],
) -> AuthenticatedCaller:
    """Authorise a policy change.

    **This is the privilege that matters most in the whole system.** An agent whose key
    can rewrite the policy governing it is not governed at all -- it can approve its own
    next action by publishing a bundle that permits it. So writing policy is a separate
    permission from calling `/v1/evaluate`, and it is not granted by holding a valid key.

    The allowlist defaults to **empty**, which means nobody may publish until an operator
    names someone. Publishing then fails with a 403 that says exactly what to set. That
    is deliberately inconvenient: the alternative default -- any authenticated caller may
    rewrite policy -- is insecure the moment a single agent key leaks, and it would be
    insecure silently.

    Reading policy stays open to any authenticated caller in the tenant. An agent
    knowing the rules it is bound by is not a risk; an agent editing them is.

    M5 replaces this with per-key roles in DynamoDB. The dependency boundary is already
    here, so that change touches one function.
    """
    allowed = _policy_admin_key_ids()

    if not allowed:
        logger.warning("policy_admin_unconfigured", extra={"key_id": caller.key_id})
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"No policy administrators are configured, so policy cannot be changed. "
                f"Set {POLICY_ADMIN_ENV} to a comma-separated list of key ids. Refusing "
                "by default is intentional: any key being able to rewrite policy would "
                "make the guardrail self-defeating."
            ),
        )

    if caller.key_id not in allowed:
        logger.warning(
            "policy_admin_denied",
            extra={"key_id": caller.key_id, "tenant_id": caller.tenant_id},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This API key may evaluate actions but may not change policy.",
        )

    return caller


PolicyAdminDependency = Annotated[AuthenticatedCaller, Depends(require_policy_admin)]
