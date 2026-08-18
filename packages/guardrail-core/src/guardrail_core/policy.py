"""Policy bundle parsing and validation.

Everything questionable about a policy is caught **here, at load time** -- unknown
operators, unknown derived facts, invalid regexes, duplicate rule ids. Evaluation must
never be the place a policy bug surfaces: at that point an agent is waiting and the safe
fallback is to block, which turns an authoring typo into an outage.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from guardrail_core.effects import Effect
from guardrail_core.extractors import EXTRACTORS
from guardrail_core.operators import OPERATORS, PolicyError

Severity = Literal["low", "medium", "high", "critical"]

_VALID_PATH_ROOTS = ("args", "derived", "context", "principal", "tool", "tenant_id", "agent_id")


class Predicate(BaseModel):
    """One condition: read a value at `path`, compare it with `op` against `value`."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    """Dotted path, e.g. `derived.record_count` or `args.table`."""

    op: str
    value: Any = None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.op not in OPERATORS:
            valid = ", ".join(sorted(OPERATORS))
            raise ValueError(f"unknown operator {self.op!r}; expected one of: {valid}")

        root = self.path.split(".", 1)[0]
        if root not in _VALID_PATH_ROOTS:
            valid = ", ".join(_VALID_PATH_ROOTS)
            raise ValueError(f"path must start with one of: {valid} (got {self.path!r})")

        # A `derived.*` path naming a fact no extractor produces would silently never
        # match, which is the most dangerous kind of policy bug: it looks like coverage.
        if root == "derived":
            fact = self.path.split(".", 1)[1] if "." in self.path else ""
            if fact not in EXTRACTORS:
                valid = ", ".join(sorted(EXTRACTORS))
                raise ValueError(f"unknown derived fact {fact!r}; available: {valid}")

        if self.op == "matches" and isinstance(self.value, str):
            try:
                re.compile(self.value)
            except re.error as exc:
                raise ValueError(f"invalid regex {self.value!r}: {exc}") from exc

        return self


class HitlOptions(BaseModel):
    """What happens while, and if, a human never answers."""

    model_config = ConfigDict(extra="forbid")

    timeout_seconds: int = Field(default=900, ge=30, le=86_400)
    on_timeout: Literal["deny", "allow"] = "deny"
    """Defaults to deny. An approval request nobody answers must not become an
    approval -- silence is not consent."""

    reviewers: list[str] = Field(default_factory=list)


class Match(BaseModel):
    """When a rule applies. `all` is AND, `any` is OR; both may be given."""

    model_config = ConfigDict(extra="forbid")

    tool: str | None = None
    """Exact name or glob (`db.*`). Omit to match every tool."""

    all: list[Predicate] = Field(default_factory=list)
    any: list[Predicate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_a_condition(self) -> Self:
        if self.tool is None and not self.all and not self.any:
            raise ValueError(
                "a rule must constrain something: set `tool`, `all`, or `any`. "
                "A match block with no conditions would apply to every action."
            )
        return self


class Rule(BaseModel):
    """One policy rule."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")]
    description: str = ""
    severity: Severity = "medium"
    match: Match
    effect: Effect
    message: str | None = None
    """Explanation returned to the agent. May interpolate `{derived.x}` / `{args.y}`."""

    hitl: HitlOptions | None = None
    enabled: bool = True
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _parse_effect(cls, data: Any) -> Any:
        """Accept the wire spelling (`block`) as well as the enum."""
        if isinstance(data, dict) and isinstance(data.get("effect"), str):
            data = {**data, "effect": Effect.from_wire(data["effect"])}
        return data

    @model_validator(mode="after")
    def _hitl_only_where_meaningful(self) -> Self:
        if self.hitl is not None and self.effect is not Effect.REQUIRE_HITL:
            raise ValueError(
                f"rule {self.id!r} sets `hitl` but its effect is {self.effect.wire_name!r}. "
                "Timeout and reviewer settings only apply to require_hitl."
            )
        return self

    @property
    def hitl_options(self) -> HitlOptions:
        """HITL settings, defaulted. Only meaningful for a require_hitl rule."""
        return self.hitl or HitlOptions()


class BundleMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundle_id: str = Field(default="default", min_length=1, max_length=100)
    version: int = Field(default=1, ge=1)
    description: str = ""
    mode: Literal["enforce", "shadow"] = "enforce"
    """`shadow` evaluates and audits without the caller acting -- bundle-wide dry run."""


class BundleDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effect: Effect = Effect.ALLOW
    """Applied when no rule matches. `allow` is default-permit, appropriate for a
    guardrail layered over an existing agent; set `block` for default-deny."""

    resolution: Literal["most_restrictive"] = "most_restrictive"
    """Only one strategy is offered, deliberately. First-match-wins makes the outcome
    depend on line order, so reordering a file during a tidy-up can silently disable a
    block rule with no error and no suspicious-looking diff."""

    @model_validator(mode="before")
    @classmethod
    def _parse_effect(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("effect"), str):
            data = {**data, "effect": Effect.from_wire(data["effect"])}
        return data


class PolicyBundle(BaseModel):
    """A complete, validated policy."""

    model_config = ConfigDict(extra="forbid")

    apiVersion: Literal["guardrail/v1"] = "guardrail/v1"  # noqa: N815 - wire field name
    metadata: BundleMetadata = Field(default_factory=BundleMetadata)
    defaults: BundleDefaults = Field(default_factory=BundleDefaults)
    rules: list[Rule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_rule_ids(self) -> Self:
        seen: set[str] = set()
        duplicates = sorted({r.id for r in self.rules if r.id in seen or seen.add(r.id)})  # type: ignore[func-returns-value]
        if duplicates:
            raise ValueError(
                f"duplicate rule id(s): {', '.join(duplicates)}. "
                "Ids appear in audit records, so they must identify exactly one rule."
            )
        return self

    @property
    def active_rules(self) -> list[Rule]:
        """Rules that are switched on. Disabled rules stay in the file for review."""
        return [rule for rule in self.rules if rule.enabled]

    @property
    def is_shadow(self) -> bool:
        return self.metadata.mode == "shadow"


def load_bundle(raw: Any) -> PolicyBundle:
    """Validate a parsed YAML/JSON document into a bundle.

    Raises PolicyError with the full validation detail. Callers surface this at startup
    or at publish time -- never mid-evaluation.
    """
    if not isinstance(raw, dict):
        raise PolicyError(f"policy bundle must be a mapping, got {type(raw).__name__}")

    try:
        return PolicyBundle.model_validate(raw)
    except Exception as exc:
        raise PolicyError(f"invalid policy bundle: {exc}") from exc


def load_bundle_yaml(text: str) -> PolicyBundle:
    """Parse and validate a YAML bundle.

    `yaml.safe_load`, never `yaml.load`: full YAML can construct arbitrary Python
    objects, which is the same class of vulnerability as `eval` on the operator set.
    """
    import yaml

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyError(f"policy bundle is not valid YAML: {exc}") from exc

    return load_bundle(raw)
