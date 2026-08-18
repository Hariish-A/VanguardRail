"""Typed view of what the guardrail returned.

Mirrors the service's response rather than reusing its Pydantic models directly: the SDK
is installed by agent teams who should not be made to depend on the service package, and
keeping the contract explicit here means a server-side refactor cannot silently change
what clients see.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DecisionName = Literal["allow", "log_and_allow", "require_hitl", "block"]


class MatchedRule(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rule_id: str
    effect: str | int
    severity: str = "medium"
    message: str | None = None


class HitlInfo(BaseModel):
    """Present only when a decision is held for human review."""

    model_config = ConfigDict(extra="ignore")

    decision_id: str
    timeout_seconds: int
    on_timeout: Literal["deny", "allow"]
    poll_url: str


class Decision(BaseModel):
    """One policy decision about one tool call.

    `extra="ignore"` on purpose: a newer service may add fields, and an SDK that refuses
    to parse them would break every agent on deploy day. Removing a field is the breaking
    change, and that is caught by the service's own contract tests.
    """

    model_config = ConfigDict(extra="ignore")

    decision: DecisionName
    allowed: bool
    matched_rules: list[MatchedRule] = Field(default_factory=list)
    message: str | None = None
    decision_id: str
    audit_seq: int = 0
    audit_hash: str = ""
    bundle_id: str = ""
    bundle_version: int = 0
    unknown_paths: list[str] = Field(default_factory=list)
    dry_run: bool = False
    hitl: HitlInfo | None = None
    latency_ms: float = 0.0

    @property
    def rule_ids(self) -> list[str]:
        return [r.rule_id for r in self.matched_rules]

    def explain(self) -> str:
        """A sentence an agent can relay to a person.

        Deliberately names the rule. "That's not allowed" invites an argument; "rule
        db-bulk-delete blocks deletions over 100 rows" tells someone what to do next.
        """
        rules = ", ".join(self.rule_ids)
        base = self.message or f"The action was {self.decision}."
        return f"{base}" + (f" (policy: {rules})" if rules else "")
