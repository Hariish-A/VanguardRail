"""Change-impact analysis: what would this policy edit actually do?

Publishing a policy change blind is the single riskiest operation in this system. A
tightened rule can halt an agent fleet; a loosened one can quietly remove a control
nobody notices is gone until an incident.

So a candidate bundle is evaluated over the same corpus of actions as the active one, and
every decision that would *change* is reported -- with a direction, because "stricter"
and "looser" are not the same risk. Stricter means availability: agents start being
refused. Looser means exposure: something that was governed no longer is.

**The honest limit, stated up front.** This compares over the actions you give it. It
proves nothing about actions absent from the corpus, so a diff showing "0 changes" means
"nothing in this corpus changed", not "this edit is safe". The corpus quality is the
analysis quality, which is why the scenario suites double as the corpus -- they are the
one set of actions someone has already thought about.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from guardrail_sim.runner import ObservedDecision, Target, TargetError, build_action
from guardrail_sim.scenarios import ScenarioSuite


@dataclass(frozen=True)
class DiffEntry:
    """One action, evaluated under both bundles."""

    scenario_id: str
    tool: str
    baseline: ObservedDecision
    candidate: ObservedDecision

    @property
    def changed(self) -> bool:
        return self.baseline.decision != self.candidate.decision

    @property
    def rules_changed(self) -> bool:
        """Whether a *different set of rules* fired, even for the same outcome.

        Worth surfacing separately: two rules blocking the same action means deleting
        one changes nothing today and everything tomorrow. A decision-only diff would
        show that edit as a no-op.
        """
        return sorted(self.baseline.rule_ids) != sorted(self.candidate.rule_ids)

    @property
    def direction(self) -> str:
        if not self.changed:
            return "unchanged"
        return "stricter" if self.candidate.strictness > self.baseline.strictness else "looser"


@dataclass
class DiffReport:
    """The full comparison."""

    entries: list[DiffEntry]
    baseline_label: str
    candidate_label: str

    @property
    def changed(self) -> list[DiffEntry]:
        return [e for e in self.entries if e.changed]

    @property
    def stricter(self) -> list[DiffEntry]:
        return [e for e in self.entries if e.direction == "stricter"]

    @property
    def looser(self) -> list[DiffEntry]:
        """Actions that would become *less* governed. The list to read first."""
        return [e for e in self.entries if e.direction == "looser"]

    @property
    def rule_only_changes(self) -> list[DiffEntry]:
        """Same outcome, different rules. Invisible to a decision-only diff."""
        return [e for e in self.entries if not e.changed and e.rules_changed]


def diff_targets(
    suites: list[ScenarioSuite],
    baseline: Target,
    candidate: Target,
    *,
    session_id: str = "guardrail-sim-diff",
) -> DiffReport:
    """Evaluate every scenario action under both targets and compare.

    Both sides run with `dry_run=True`. Change-impact analysis must never be capable of
    causing the change it is analysing -- and against a live endpoint that also keeps the
    probe decisions out of the enforcement metrics.
    """
    entries: list[DiffEntry] = []

    for suite in suites:
        for scenario in suite.active:
            action: dict[str, Any] = build_action(
                suite, scenario, dry_run=True, session_id=session_id
            )
            entries.append(
                DiffEntry(
                    scenario_id=scenario.id,
                    tool=scenario.action.tool,
                    baseline=baseline.evaluate(action),
                    candidate=candidate.evaluate(action),
                )
            )

    return DiffReport(
        entries=entries,
        baseline_label=baseline.description,
        candidate_label=candidate.description,
    )


class SimulateTarget:
    """A target backed by `POST /v1/simulate` on a deployed control plane.

    Used to diff against whatever policy is *actually live*, rather than against a file
    someone believes is live. Those differ more often than anyone expects -- that gap is
    precisely what a policy-versioning story exists to close.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        bundle: dict[str, Any] | None = None,
        version: int | None = None,
        timeout: float = 15.0,
        transport: Any = None,
    ) -> None:
        import httpx

        if bundle is not None and version is not None:
            raise TargetError("pass a candidate bundle or a candidate version, not both")

        self.base_url = base_url.rstrip("/")
        self._bundle = bundle
        self._version = version
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={"x-api-key": api_key, "content-type": "application/json"},
            transport=transport,
        )

    @property
    def description(self) -> str:
        if self._bundle is not None:
            meta = self._bundle.get("metadata", {}) if isinstance(self._bundle, dict) else {}
            return f"candidate bundle {meta.get('bundle_id', '?')} (inline) via {self.base_url}"
        if self._version is not None:
            return f"published version {self._version} via {self.base_url}"
        return f"active policy via {self.base_url}"

    def close(self) -> None:
        self._http.close()

    def evaluate(self, action: dict[str, Any]) -> ObservedDecision:
        import httpx

        payload: dict[str, Any] = {"action": action}
        if self._bundle is not None:
            payload["bundle"] = self._bundle
        elif self._version is not None:
            payload["version"] = self._version

        try:
            response = self._http.post("/v1/simulate", json=payload)
        except httpx.HTTPError as exc:
            raise TargetError(f"could not reach {self.base_url}: {exc}") from exc

        if response.status_code == 401:
            raise TargetError("the endpoint rejected the API key (401)")
        if response.status_code >= 400:
            raise TargetError(
                f"/v1/simulate returned {response.status_code}: {response.text[:300]}"
            )

        body = response.json()
        return ObservedDecision(
            decision=str(body.get("decision", "")),
            allowed=bool(body.get("allowed", False)),
            rule_ids=[r.get("rule_id", "") for r in body.get("matched_rules", [])],
            message=body.get("message"),
            unknown_paths=list(body.get("unknown_paths", [])),
            bundle_id=str(body.get("bundle_id", "")),
            bundle_version=int(body.get("bundle_version", 0)),
            dry_run=True,
            latency_ms=float(body.get("latency_ms", 0.0)),
        )


def render_diff_console(report: DiffReport) -> str:
    """Human summary of a change-impact analysis."""
    lines = [
        "",
        "Policy change impact",
        f"  baseline  : {report.baseline_label}",
        f"  candidate : {report.candidate_label}",
        f"  corpus    : {len(report.entries)} actions",
        "",
    ]

    if not report.changed and not report.rule_only_changes:
        lines.extend(
            [
                f"  No decision in this corpus of {len(report.entries)} actions would change.",
                "  That is not the same as 'safe': it means nothing here exercised the edit.",
                "",
            ]
        )
        return "\n".join(lines)

    if report.looser:
        lines.append(f"  LOOSER -- {len(report.looser)} action(s) would become less governed:")
        for entry in report.looser:
            lines.append(
                f"    {entry.scenario_id} ({entry.tool}): "
                f"{entry.baseline.decision} -> {entry.candidate.decision}"
            )
            lines.append(f"      rules {_transition(entry)}")
        lines.append("")

    if report.stricter:
        lines.append(f"  STRICTER -- {len(report.stricter)} action(s) would be restrained more:")
        for entry in report.stricter:
            lines.append(
                f"    {entry.scenario_id} ({entry.tool}): "
                f"{entry.baseline.decision} -> {entry.candidate.decision}"
            )
            lines.append(f"      rules {_transition(entry)}")
        lines.append("")

    if report.rule_only_changes:
        lines.append(
            f"  SAME OUTCOME, DIFFERENT RULES -- {len(report.rule_only_changes)} action(s). "
            "The decision holds only because another rule covers it:"
        )
        for entry in report.rule_only_changes:
            lines.append(
                f"    {entry.scenario_id} ({entry.tool}): {entry.baseline.decision} via "
                f"{_rules(entry.baseline.rule_ids)} -> {_rules(entry.candidate.rule_ids)}"
            )
        lines.append("")

    return "\n".join(lines)


def _rules(rule_ids: list[str]) -> str:
    return ", ".join(rule_ids) if rule_ids else "(none)"


def _transition(entry: DiffEntry) -> str:
    return f"{_rules(entry.baseline.rule_ids)} -> {_rules(entry.candidate.rule_ids)}"
