"""Executing scenarios against a policy.

Two targets, one set of scenario files:

* **Offline** evaluates with `guardrail_core.engine` in-process. No AWS, no network, no
  credentials -- which is what lets the conformance suite run on every pull request
  instead of only after a deploy.
* **Live** calls `POST /v1/evaluate` on a deployed control plane. Same scenarios, same
  assertions, but it exercises auth, storage, the audit write, and the actually-active
  policy bundle.

Keeping both behind one interface is deliberate. If the two could drift, a green offline
report would say nothing about production, and the CI gate would be theatre. The
`parity` command exists to prove they have not drifted.

**The harness never executes a tool.** It asks what policy *would* say. That is why it is
safe to point at production, and it is also the honest limit of what it proves: it
verifies the decision, not the enforcement. The demo agent's side-effect ledger covers
the other half.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from guardrail_sim.scenarios import Scenario, ScenarioSuite

# The four decision names, ordered from most permissive to most restrictive. Used to
# describe a policy change as tightening or loosening rather than merely "different" --
# which is the distinction a reviewer actually cares about.
_STRICTNESS: dict[str, int] = {
    "allow": 0,
    "log_and_allow": 1,
    "require_hitl": 2,
    "block": 3,
}


@dataclass(frozen=True)
class ObservedDecision:
    """What a target said about one action, normalised across targets."""

    decision: str
    allowed: bool
    rule_ids: list[str] = field(default_factory=list)
    message: str | None = None
    unknown_paths: list[str] = field(default_factory=list)
    bundle_id: str = ""
    bundle_version: int = 0
    decision_id: str = ""
    dry_run: bool = False
    latency_ms: float = 0.0

    @property
    def strictness(self) -> int:
        return _STRICTNESS.get(self.decision, 0)


class Target(Protocol):
    """Something that can answer "what would policy say about this action?"."""

    @property
    def description(self) -> str: ...

    def evaluate(self, action: dict[str, Any]) -> ObservedDecision: ...


class TargetError(RuntimeError):
    """The target could not be reached or refused the request.

    Distinct from a scenario failure. A scenario failure means the policy is wrong; this
    means the run itself is invalid, and reporting it as "tests failed" would send
    someone to edit a policy file over what is actually a bad URL or an expired key.
    """


# ---------------------------------------------------------------------------
# Offline
# ---------------------------------------------------------------------------


class OfflineTarget:
    """Evaluates in-process against a loaded bundle.

    Reproduces the one piece of behaviour that lives in the service rather than the
    engine -- shadow-mode downgrading -- so an offline run of a shadow bundle agrees
    with a live one. A parity check that quietly compared different semantics would be
    worse than no parity check.
    """

    def __init__(self, bundle: Any, *, label: str = "offline") -> None:
        self._bundle = bundle
        self._label = label

    @property
    def description(self) -> str:
        meta = self._bundle.metadata
        return f"{self._label} (bundle {meta.bundle_id} v{meta.version}, mode={meta.mode})"

    def evaluate(self, action: dict[str, Any]) -> ObservedDecision:
        from guardrail_core.effects import Effect
        from guardrail_core.engine import evaluate as engine_evaluate
        from guardrail_core.models import ActionEnvelope

        started = time.perf_counter()
        envelope = ActionEnvelope.model_validate(action)
        result = engine_evaluate(envelope, self._bundle)

        effective = result.effect
        if self._bundle.is_shadow and effective in (Effect.BLOCK, Effect.REQUIRE_HITL):
            effective = Effect.LOG_AND_ALLOW

        return ObservedDecision(
            decision=effective.wire_name,
            allowed=effective in (Effect.ALLOW, Effect.LOG_AND_ALLOW),
            rule_ids=[m.rule_id for m in result.matched_rules],
            message=result.message,
            unknown_paths=list(result.unknown_paths),
            bundle_id=result.bundle_id,
            bundle_version=result.bundle_version,
            dry_run=bool(envelope.dry_run),
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
        )


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------


class LiveTarget:
    """Calls a deployed control plane over HTTPS."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 15.0,
        transport: Any = None,
    ) -> None:
        import httpx

        if not base_url:
            raise TargetError("no endpoint given; pass --endpoint or set GUARDRAIL_BASE_URL")
        if not api_key:
            raise TargetError(
                "no API key given; pass --api-key or set GUARDRAIL_API_KEY. The live "
                "endpoint rejects unauthenticated callers by design."
            )

        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={"x-api-key": api_key, "content-type": "application/json"},
            transport=transport,
        )

    @property
    def description(self) -> str:
        return f"live {self.base_url}"

    def close(self) -> None:
        self._http.close()

    def evaluate(self, action: dict[str, Any]) -> ObservedDecision:
        import httpx

        # A fresh key per call. Reusing one would make the second run of a suite return
        # the first run's cached decision, so a policy change would appear to have had
        # no effect -- a silently stale green report.
        payload = {**action, "idempotency_key": str(uuid.uuid4())}

        try:
            response = self._http.post("/v1/evaluate", json=payload)
        except httpx.HTTPError as exc:
            raise TargetError(f"could not reach {self.base_url}: {exc}") from exc

        if response.status_code == 401:
            raise TargetError("the endpoint rejected the API key (401)")
        if response.status_code >= 400:
            raise TargetError(f"endpoint returned {response.status_code}: {response.text[:300]}")

        body = response.json()
        return ObservedDecision(
            decision=str(body.get("decision", "")),
            allowed=bool(body.get("allowed", False)),
            rule_ids=[r.get("rule_id", "") for r in body.get("matched_rules", [])],
            message=body.get("message"),
            unknown_paths=list(body.get("unknown_paths", [])),
            bundle_id=str(body.get("bundle_id", "")),
            bundle_version=int(body.get("bundle_version", 0)),
            decision_id=str(body.get("decision_id", "")),
            dry_run=bool(body.get("dry_run", False)),
            # The server's own measurement of engine time, not the round trip. Network
            # latency belongs in the load test, not in a policy conformance report.
            latency_ms=float(body.get("latency_ms", 0.0)),
        )


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------


def check(scenario: Scenario, observed: ObservedDecision) -> list[str]:
    """Compare an observed decision against a scenario's expectation.

    Returns *every* failure rather than the first. Fixing a policy one assertion per run
    is a slow loop, and the full list usually shows the single underlying cause.
    """
    expect = scenario.expect
    failures: list[str] = []

    if expect.decision is not None and observed.decision != expect.decision:
        matched = ", ".join(observed.rule_ids)
        failures.append(
            f"expected decision {expect.decision!r}, got {observed.decision!r}"
            + (f" (matched: {matched})" if matched else " (no rules matched)")
        )

    if expect.allowed is not None and observed.allowed != expect.allowed:
        failures.append(f"expected allowed={expect.allowed}, got allowed={observed.allowed}")

    missing = [r for r in expect.rules if r not in observed.rule_ids]
    if missing:
        failures.append(
            f"expected rule(s) {', '.join(missing)} to match; matched "
            f"{', '.join(observed.rule_ids) or 'nothing'}"
        )

    present = [r for r in expect.rules_absent if r in observed.rule_ids]
    if present:
        failures.append(f"rule(s) {', '.join(present)} matched but should not have")

    if expect.message_contains is not None:
        message = observed.message or ""
        if expect.message_contains not in message:
            failures.append(
                f"expected the message to contain {expect.message_contains!r}; got "
                f"{message[:200]!r}"
            )

    missing_unknown = [p for p in expect.unknown_paths if p not in observed.unknown_paths]
    if missing_unknown:
        failures.append(
            f"expected path(s) {', '.join(missing_unknown)} to be reported UNKNOWN; "
            f"reported {', '.join(observed.unknown_paths) or 'none'}"
        )

    return failures


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class ScenarioResult:
    """One scenario's outcome."""

    scenario: Scenario
    suite_name: str
    observed: ObservedDecision | None
    failures: list[str]
    duration_ms: float
    error: str | None = None
    """Set when the target itself failed. Reported as an *error*, not a failure --
    a broken endpoint is not a policy regression, and conflating them wastes the
    reader's time."""

    @property
    def passed(self) -> bool:
        return not self.failures and self.error is None


@dataclass
class RunReport:
    """Everything one run produced."""

    results: list[ScenarioResult]
    target: str
    mode: str
    started_at: str
    duration_s: float
    dry_run: bool = False

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.failures and r.error is None)

    @property
    def errored(self) -> int:
        return sum(1 for r in self.results if r.error is not None)

    @property
    def critical_failures(self) -> list[ScenarioResult]:
        """Failures on scenarios encoding a stated success criterion."""
        return [r for r in self.results if not r.passed and r.scenario.critical]

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.errored == 0

    @property
    def exit_code(self) -> int:
        """0 only when everything passed. This is what makes it a gate."""
        return 0 if self.ok else 1


def run_suites(
    suites: list[ScenarioSuite],
    target: Target,
    *,
    dry_run: bool = False,
    session_id: str | None = None,
) -> RunReport:
    """Execute every enabled scenario and collect the results.

    A target error does not abort the run. The remaining scenarios are recorded as
    errored too, so the report shows the whole picture rather than stopping at the first
    symptom -- but the exit code is still non-zero, so nothing is quietly tolerated.
    """
    run_session = session_id or f"sim-{uuid.uuid4().hex[:12]}"
    started = time.perf_counter()
    started_at = datetime.now(UTC).isoformat(timespec="seconds")

    results: list[ScenarioResult] = []

    for suite in suites:
        for scenario in suite.active:
            action = build_action(suite, scenario, dry_run=dry_run, session_id=run_session)

            case_started = time.perf_counter()
            try:
                observed = target.evaluate(action)
            except TargetError as exc:
                results.append(
                    ScenarioResult(
                        scenario=scenario,
                        suite_name=suite.name,
                        observed=None,
                        failures=[],
                        duration_ms=round((time.perf_counter() - case_started) * 1000, 3),
                        error=str(exc),
                    )
                )
                continue

            results.append(
                ScenarioResult(
                    scenario=scenario,
                    suite_name=suite.name,
                    observed=observed,
                    failures=check(scenario, observed),
                    duration_ms=round((time.perf_counter() - case_started) * 1000, 3),
                )
            )

    return RunReport(
        results=results,
        target=target.description,
        mode="dry-run" if dry_run else "enforce",
        started_at=started_at,
        duration_s=round(time.perf_counter() - started, 3),
        dry_run=dry_run,
    )


def build_action(
    suite: ScenarioSuite,
    scenario: Scenario,
    *,
    dry_run: bool,
    session_id: str,
) -> dict[str, Any]:
    """Turn a scenario into an action envelope.

    Suite defaults sit *under* the scenario's own context, never over it, so a scenario
    can always override an inherited value. The reverse would make a scenario's stated
    context a lie.
    """
    return {
        "agent_id": suite.defaults.agent_id,
        "session_id": suite.defaults.session_id or session_id,
        "tool": scenario.action.tool,
        "arguments": scenario.action.arguments,
        "context": {**suite.defaults.context, **scenario.action.context},
        "principal": scenario.action.principal,
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# Dry-run parity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParityFinding:
    """One scenario where dry-run and enforcement disagreed."""

    scenario_id: str
    enforce_decision: str
    dry_run_decision: str


@dataclass
class ParityReport:
    """Whether dry-run reports what enforcement would actually do.

    This is the claim that makes dry-run worth anything. A shadow run that quietly
    evaluated differently would give operators false confidence before a policy change,
    which is worse than having no dry-run at all.
    """

    enforce: RunReport
    dry: RunReport
    findings: list[ParityFinding]

    @property
    def ok(self) -> bool:
        return not self.findings and self.enforce.ok and self.dry.ok

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1


def run_parity(suites: list[ScenarioSuite], target: Target) -> ParityReport:
    """Run every scenario twice -- enforcing and dry-run -- and diff the decisions."""
    enforce = run_suites(suites, target, dry_run=False)
    dry = run_suites(suites, target, dry_run=True)

    by_id = {r.scenario.id: r for r in dry.results}
    findings: list[ParityFinding] = []

    for enforced in enforce.results:
        shadowed = by_id.get(enforced.scenario.id)
        if shadowed is None or enforced.observed is None or shadowed.observed is None:
            continue
        if enforced.observed.decision != shadowed.observed.decision:
            findings.append(
                ParityFinding(
                    scenario_id=enforced.scenario.id,
                    enforce_decision=enforced.observed.decision,
                    dry_run_decision=shadowed.observed.decision,
                )
            )

    return ParityReport(enforce=enforce, dry=dry, findings=findings)
