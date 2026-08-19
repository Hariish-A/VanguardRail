"""`GET /v1/me` — what the presented key is, and what it may do.

Exists for the console. A UI that guesses at permissions gets them wrong in one of two
ways, and both are bad:

* **Guess too permissive** and it renders an Approve button that always 403s. The reviewer
  learns the control is broken rather than that they lack the role.
* **Guess too restrictive** and it hides a control the operator legitimately holds, which
  during an incident is the more expensive mistake.

So the server states it. The client renders what this says and nothing more.

`capabilities` is deliberately a flat set of verbs rather than the role name alone. The
console asks "may I resolve?", not "am I a reviewer?" — and keeping the mapping on the
server means a future role, or a break-glass grant that is not a role at all, changes one
place. `GUARDRAIL_POLICY_ADMIN_KEY_IDS` is exactly such a grant: a key with the `agent`
role named in that variable really can publish policy, and a client deriving capabilities
from the role string alone would report otherwise.

**No credential is echoed.** The response names the key by its `key_id` and never returns
the key, its hash, or any part of it — this endpoint is reached with the key in a header,
so returning it would only create a new place for it to leak.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from guardrail_service.auth import (
    ADMIN,
    REVIEWER,
    AuthenticatedCaller,
    _policy_admin_key_ids,
    require_api_key,
)
from guardrail_service.config import get_settings

router = APIRouter(prefix="/v1", tags=["identity"])

EVALUATE = "evaluate"
SIMULATE = "simulate"
READ_AUDIT = "read_audit"
READ_DECISIONS = "read_decisions"
RESOLVE_DECISIONS = "resolve_decisions"
READ_POLICY = "read_policy"
PUBLISH_POLICY = "publish_policy"


class Identity(BaseModel):
    """Who is calling, and what that entitles them to."""

    key_id: str
    """The identifier recorded against every decision this key causes. Not the key."""

    tenant_id: str
    name: str
    role: str

    capabilities: list[str] = Field(default_factory=list)
    """Verbs this caller may perform, sorted. The console gates its UI on these."""

    stage: str
    version: str


def _capabilities(caller: AuthenticatedCaller) -> list[str]:
    """Resolve the caller's role — and any break-glass grant — into verbs.

    Kept in step with the dependencies that actually enforce them: `require_reviewer`
    for resolution and `require_policy_admin` for publication. A drift between this list
    and those checks is a UI that lies, so `test_me.py` asserts the two agree by making
    the real requests rather than by re-reading this function.
    """
    verbs = [EVALUATE, SIMULATE, READ_AUDIT, READ_DECISIONS, READ_POLICY]

    if caller.can(REVIEWER):
        verbs.append(RESOLVE_DECISIONS)

    # Mirrors require_policy_admin: the role grants it, and so does being named in the
    # environment allowlist. Reporting only the role would tell a break-glass admin they
    # cannot do the thing they can in fact do.
    if caller.can(ADMIN) or caller.key_id in _policy_admin_key_ids():
        verbs.append(PUBLISH_POLICY)

    return sorted(verbs)


@router.get(
    "/me",
    response_model=Identity,
    summary="Identity and permissions of the presented API key",
    dependencies=[Depends(require_api_key)],
)
async def whoami(caller: Annotated[AuthenticatedCaller, Depends(require_api_key)]) -> Identity:
    """Describe the caller. Requires a valid key, so it doubles as a credential check —
    which is how the console verifies a pasted key before storing it."""
    settings = get_settings()
    return Identity(
        key_id=caller.key_id,
        tenant_id=caller.tenant_id,
        name=caller.name,
        role=caller.role,
        capabilities=_capabilities(caller),
        stage=settings.stage,
        version=settings.version,
    )
