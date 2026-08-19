"""The scenario DSL.

A scenario file is the executable form of the sentence "this policy should do X". It
names an action, states the expected decision, and -- crucially -- states *which rule*
should produce it.

Asserting only the outcome is a trap worth naming. A scenario that expects `block` keeps
passing after the rule it was written for is deleted, so long as some other rule happens
to block the same action. That is exactly the policy regression the suite exists to
catch, sailing through green. So the shipped suites assert rule ids, and a scenario that
genuinely does not care has to say so.

Everything here is pure data validation: the same file drives an offline run against a
local bundle and a live run against deployed AWS, so a green CI report and a green
production report mean the same thing.
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

DecisionName = Literal["allow", "log_and_allow", "require_hitl", "block"]


class ScenarioAction(BaseModel):
    """The tool call under test."""

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    principal: dict[str, Any] | None = None


class Expectation(BaseModel):
    """What the policy is supposed to say about the action.

    Every field is optional so a scenario can assert exactly as much as it means. A
    scenario asserting nothing at all is rejected, though: it would be a test that
    cannot fail, which is worse than no test because it reads like coverage.
    """

    model_config = ConfigDict(extra="forbid")

    decision: DecisionName | None = None

    allowed: bool | None = None
    """Whether the caller may dispatch the tool right now.

    Kept separate from `decision` because `allow` and `log_and_allow` differ in intent
    but agree here -- and a scenario often cares about only one of the two.
    """

    rules: list[str] = Field(default_factory=list)
    """Rule ids that must all appear among the matched rules.

    A subset check rather than equality: adding an unrelated rule to the bundle should
    not break every existing scenario. Use `rules_absent` for exclusions that matter.
    """

    rules_absent: list[str] = Field(default_factory=list)
    """Rule ids that must NOT have matched. This is how an over-broad rule gets pinned."""

    message_contains: str | None = None
    """Substring the returned explanation must contain.

    The message is what a person or an agent actually reads, so it is part of the
    contract -- a block that cannot explain itself becomes a support ticket.
    """

    unknown_paths: list[str] = Field(default_factory=list)
    """Fact paths the engine must report as undeterminable.

    Worth asserting explicitly: an UNKNOWN quietly disappearing means an extractor
    started guessing, and the fail-closed behaviour that depended on it is gone.
    """

    @model_validator(mode="after")
    def _must_assert_something(self) -> Self:
        if not any(
            (
                self.decision is not None,
                self.allowed is not None,
                self.rules,
                self.rules_absent,
                self.message_contains is not None,
                self.unknown_paths,
            )
        ):
            raise ValueError(
                "expectation asserts nothing, so the scenario can never fail. Set at "
                "least one of: decision, allowed, rules, rules_absent, "
                "message_contains, unknown_paths."
            )
        return self


class Scenario(BaseModel):
    """One action and its expected verdict."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str = ""
    action: ScenarioAction
    expect: Expectation
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True

    critical: bool = False
    """Marks a scenario encoding a stated success criterion rather than a nice-to-have.

    Reported separately, so "12 of 14 passed" cannot obscure the fact that one of the
    two failures was a requirement the problem statement names explicitly.
    """


class ScenarioDefaults(BaseModel):
    """Identity applied to every action in the file, so scenarios stay about policy."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str = "guardrail-sim"

    session_id: str | None = None
    """Unset by default: the runner generates one per run, which keeps each run's audit
    records separately queryable through the session index."""

    context: dict[str, Any] = Field(default_factory=dict)
    """Merged *under* each scenario's own context, so a scenario can always override."""


class ScenarioSuite(BaseModel):
    """A validated scenario file."""

    model_config = ConfigDict(extra="forbid")

    apiVersion: Literal["guardrail/v1"] = "guardrail/v1"  # noqa: N815 - wire field name
    name: str = Field(default="unnamed suite", min_length=1)
    description: str = ""
    defaults: ScenarioDefaults = Field(default_factory=ScenarioDefaults)
    scenarios: list[Scenario] = Field(default_factory=list)

    source: str = ""
    """Path the suite was loaded from. Set by the loader, never by the file itself."""

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        seen: set[str] = set()
        duplicates = sorted({s.id for s in self.scenarios if s.id in seen or seen.add(s.id)})  # type: ignore[func-returns-value]
        if duplicates:
            raise ValueError(
                f"duplicate scenario id(s): {', '.join(duplicates)}. "
                "Ids identify results in the report, so they must be unique."
            )
        return self

    @property
    def active(self) -> list[Scenario]:
        return [s for s in self.scenarios if s.enabled]


class ScenarioError(ValueError):
    """A scenario file is malformed. Raised at load time, never mid-run."""


def load_suite(text: str, *, source: str = "") -> ScenarioSuite:
    """Parse and validate one scenario file.

    `yaml.safe_load` only. Scenario files are as untrusted as policy files, and full
    YAML can construct arbitrary Python objects.
    """
    import yaml

    label = source or "scenario file"

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{label} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ScenarioError(f"{label} must be a mapping, got {type(raw).__name__}")

    try:
        return ScenarioSuite.model_validate({**raw, "source": source})
    except Exception as exc:
        raise ScenarioError(f"invalid scenario file {label}: {exc}") from exc


def load_suites(paths: list[str]) -> list[ScenarioSuite]:
    """Load every scenario file under the given files or directories.

    Sorted, so report ordering is stable between runs. A diffable report is worth more
    than a fast one, and unordered output makes every run look changed.

    An empty result raises rather than reporting a cheerful zero-scenario pass. "0 tests,
    all green" is the most misleading thing a conformance gate can print.
    """
    from pathlib import Path

    files: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_dir():
            files.extend(sorted(path.rglob("*.yaml")))
            files.extend(sorted(path.rglob("*.yml")))
        elif path.is_file():
            files.append(path)
        else:
            raise ScenarioError(f"no such scenario path: {raw_path}")

    suites = [load_suite(f.read_text(encoding="utf-8"), source=str(f)) for f in sorted(set(files))]

    if sum(len(s.active) for s in suites) == 0:
        raise ScenarioError(
            f"no enabled scenarios found in {', '.join(paths)}. Refusing to report a "
            "vacuous pass -- an empty conformance run is a broken gate, not a green one."
        )

    return suites
