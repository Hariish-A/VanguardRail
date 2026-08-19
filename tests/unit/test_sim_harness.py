"""The simulation harness.

The load-bearing test in this file is `test_the_suite_actually_fails_when_policy_regresses`.
A conformance suite that reports green is worth exactly as much as the confidence that
it would report red -- so the suite is run against a deliberately broken policy and
required to notice. Without that, every other assertion here is decoration.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pytest
import yaml
from guardrail_core.policy import load_bundle, load_bundle_yaml
from guardrail_sim.diffing import diff_targets
from guardrail_sim.report import render_html, render_json, render_junit
from guardrail_sim.runner import (
    ObservedDecision,
    OfflineTarget,
    check,
    run_parity,
    run_suites,
)
from guardrail_sim.scenarios import (
    Scenario,
    ScenarioError,
    load_suite,
    load_suites,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_DIR = REPO_ROOT / "scenarios"
POLICY_FILE = REPO_ROOT / "policies" / "default.yaml"


@pytest.fixture
def policy_document() -> dict[str, Any]:
    return dict(yaml.safe_load(POLICY_FILE.read_text(encoding="utf-8")))


@pytest.fixture
def suites() -> list[Any]:
    return load_suites([str(SCENARIO_DIR)])


@pytest.fixture
def target() -> OfflineTarget:
    return OfflineTarget(load_bundle_yaml(POLICY_FILE.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# The suite itself
# ---------------------------------------------------------------------------


def test_shipped_scenarios_pass_against_the_shipped_policy(
    suites: list[Any], target: OfflineTarget
) -> None:
    report = run_suites(suites, target)

    assert report.ok, [(r.scenario.id, r.failures, r.error) for r in report.results if not r.passed]
    assert report.total >= 15
    assert report.exit_code == 0


def test_the_five_stated_success_criteria_are_all_marked_critical(suites: list[Any]) -> None:
    """The problem statement's five outcomes must be distinguishable from nice-to-haves,
    or a partial pass reads as a good result."""
    criteria = {
        s.id
        for suite in suites
        for s in suite.active
        if s.critical and "success criteria" in suite.name.lower()
    }

    assert criteria == {
        "bulk-delete-blocked",
        "small-delete-allowed",
        "external-email-held-for-review",
        "internal-email-allowed",
        "confidential-read-recorded",
    }


def test_the_suite_actually_fails_when_policy_regresses(
    suites: list[Any], policy_document: dict[str, Any]
) -> None:
    """**The test that makes the rest of this file mean something.**

    Delete the bulk-delete rule and the suite must go red, name the scenario, and exit
    non-zero. A gate that cannot fail is not a gate, and the only way to know is to
    break the thing it guards.
    """
    broken = copy.deepcopy(policy_document)
    broken["rules"] = [r for r in broken["rules"] if r["id"] != "db-bulk-delete"]

    report = run_suites(suites, OfflineTarget(load_bundle(broken)))

    assert not report.ok
    assert report.exit_code == 1

    failed = {r.scenario.id for r in report.results if not r.passed}
    assert "bulk-delete-blocked" in failed

    # And it must be reported as a *stated criterion*, not buried in the count.
    assert "bulk-delete-blocked" in {r.scenario.id for r in report.critical_failures}


def test_a_loosened_threshold_is_caught(suites: list[Any], policy_document: dict[str, Any]) -> None:
    """Deleting a rule is the obvious regression. Quietly raising a threshold is the
    realistic one -- the rule is still there, still matching, just not on this action."""
    loosened = copy.deepcopy(policy_document)
    for rule in loosened["rules"]:
        if rule["id"] == "db-bulk-delete":
            rule["match"]["all"][0]["value"] = 10_000

    report = run_suites(suites, OfflineTarget(load_bundle(loosened)))

    assert not report.ok
    assert "bulk-delete-blocked" in {r.scenario.id for r in report.results if not r.passed}


# ---------------------------------------------------------------------------
# Scenario DSL
# ---------------------------------------------------------------------------


def test_an_expectation_asserting_nothing_is_rejected() -> None:
    """A scenario that cannot fail is worse than no scenario: it reads like coverage."""
    with pytest.raises(ScenarioError, match="asserts nothing"):
        load_suite(
            """
            apiVersion: guardrail/v1
            name: bad
            scenarios:
              - id: empty-expectation
                action: {tool: file.read, arguments: {path: /tmp/x}}
                expect: {}
            """,
            source="test",
        )


def test_duplicate_scenario_ids_are_rejected() -> None:
    with pytest.raises(ScenarioError, match="duplicate scenario id"):
        load_suite(
            """
            apiVersion: guardrail/v1
            name: dupes
            scenarios:
              - id: same
                action: {tool: file.read, arguments: {}}
                expect: {decision: allow}
              - id: same
                action: {tool: file.read, arguments: {}}
                expect: {decision: allow}
            """,
            source="test",
        )


def test_unknown_scenario_fields_are_rejected() -> None:
    """`extra=forbid` throughout: a typo like `expects:` would otherwise silently
    produce a scenario with no assertions."""
    with pytest.raises(ScenarioError):
        load_suite(
            """
            apiVersion: guardrail/v1
            name: typo
            scenarios:
              - id: typo-case
                action: {tool: file.read, arguments: {}}
                expects: {decision: allow}
            """,
            source="test",
        )


def test_an_empty_scenario_set_refuses_to_report_a_pass(tmp_path: Path) -> None:
    """'0 tests, all green' is the most misleading thing a gate can print."""
    (tmp_path / "empty.yaml").write_text(
        "apiVersion: guardrail/v1\nname: nothing\nscenarios: []\n", encoding="utf-8"
    )

    with pytest.raises(ScenarioError, match="vacuous pass"):
        load_suites([str(tmp_path)])


def test_suite_defaults_never_override_a_scenarys_own_context() -> None:
    """The reverse would make a scenario's stated context a lie."""
    from guardrail_sim.runner import build_action

    suite = load_suite(
        """
        apiVersion: guardrail/v1
        name: defaults
        defaults:
          agent_id: default-agent
          context: {environment: staging, region: eu}
        scenarios:
          - id: overrides
            action:
              tool: db.delete_records
              arguments: {count: 5}
              context: {environment: production}
            expect: {decision: require_hitl}
        """,
        source="test",
    )

    action = build_action(suite, suite.active[0], dry_run=False, session_id="s")

    assert action["context"] == {"environment": "production", "region": "eu"}
    assert action["agent_id"] == "default-agent"


# ---------------------------------------------------------------------------
# Assertion logic
# ---------------------------------------------------------------------------


def _scenario(**expect: Any) -> Scenario:
    return Scenario.model_validate(
        {
            "id": "case",
            "action": {"tool": "db.delete_records", "arguments": {"count": 5}},
            "expect": expect,
        }
    )


def test_check_reports_every_failure_not_just_the_first() -> None:
    """Fixing a policy one assertion per run is a slow loop, and the full list usually
    shows the single underlying cause."""
    observed = ObservedDecision(decision="allow", allowed=True, rule_ids=["other-rule"])

    failures = check(
        _scenario(decision="block", allowed=False, rules=["db-bulk-delete"]),
        observed,
    )

    assert len(failures) == 3


def test_check_catches_a_rule_that_should_not_have_matched() -> None:
    observed = ObservedDecision(decision="block", allowed=False, rule_ids=["over-broad"])

    failures = check(_scenario(decision="block", rules_absent=["over-broad"]), observed)

    assert len(failures) == 1
    assert "should not have" in failures[0]


def test_check_catches_a_disappearing_unknown() -> None:
    """An UNKNOWN quietly vanishing means an extractor started guessing, and the
    fail-closed behaviour that depended on it is gone."""
    observed = ObservedDecision(decision="block", allowed=False, unknown_paths=[])

    failures = check(_scenario(unknown_paths=["derived.record_count"]), observed)

    assert len(failures) == 1
    assert "UNKNOWN" in failures[0]


def test_check_passes_when_everything_matches() -> None:
    observed = ObservedDecision(
        decision="block",
        allowed=False,
        rule_ids=["db-bulk-delete", "extra-rule"],
        message="Blocked: 500 records",
    )

    assert (
        check(
            _scenario(
                decision="block",
                allowed=False,
                rules=["db-bulk-delete"],
                message_contains="500",
            ),
            observed,
        )
        == []
    )


# ---------------------------------------------------------------------------
# Dry-run parity
# ---------------------------------------------------------------------------


def test_dry_run_reports_the_same_decisions_as_enforcement(
    suites: list[Any], target: OfflineTarget
) -> None:
    """The claim that makes dry-run worth anything. A shadow run that evaluated
    differently would give false confidence before a policy change."""
    parity = run_parity(suites, target)

    assert parity.findings == []
    assert parity.ok
    assert parity.dry.dry_run is True
    assert parity.enforce.dry_run is False


def test_dry_run_marks_every_decision_as_a_dry_run(
    suites: list[Any], target: OfflineTarget
) -> None:
    """Dry-run records must be distinguishable, or they contaminate enforcement metrics."""
    report = run_suites(suites, target, dry_run=True)

    assert all(r.observed is not None and r.observed.dry_run for r in report.results)


# ---------------------------------------------------------------------------
# Change-impact diffing
# ---------------------------------------------------------------------------


def test_removing_a_rule_shows_up_as_looser(
    suites: list[Any], policy_document: dict[str, Any], target: OfflineTarget
) -> None:
    candidate = copy.deepcopy(policy_document)
    candidate["rules"] = [r for r in candidate["rules"] if r["id"] != "db-bulk-delete"]

    report = diff_targets(suites, target, OfflineTarget(load_bundle(candidate)))

    assert report.looser, "removing a block rule must be reported as loosening"
    assert "bulk-delete-blocked" in {e.scenario_id for e in report.looser}
    assert not report.stricter


def test_tightening_a_threshold_shows_up_as_stricter(
    suites: list[Any], policy_document: dict[str, Any], target: OfflineTarget
) -> None:
    candidate = copy.deepcopy(policy_document)
    for rule in candidate["rules"]:
        if rule["id"] == "db-bulk-delete":
            rule["match"]["all"][0]["value"] = 1

    report = diff_targets(suites, target, OfflineTarget(load_bundle(candidate)))

    assert report.stricter
    assert "small-delete-allowed" in {e.scenario_id for e in report.stricter}


def test_an_identical_bundle_diffs_to_nothing(
    suites: list[Any], policy_document: dict[str, Any], target: OfflineTarget
) -> None:
    report = diff_targets(suites, target, OfflineTarget(load_bundle(policy_document)))

    assert report.changed == []
    assert report.rule_only_changes == []


def test_a_redundant_rule_removal_is_reported_even_though_the_decision_holds(
    suites: list[Any], policy_document: dict[str, Any], target: OfflineTarget
) -> None:
    """Two rules blocking the same action means deleting one changes nothing today and
    everything tomorrow. A decision-only diff would call that a no-op."""
    candidate = copy.deepcopy(policy_document)
    candidate["rules"] = [r for r in candidate["rules"] if r["id"] != "confidential-read-audit"]

    report = diff_targets(suites, target, OfflineTarget(load_bundle(candidate)))

    rule_only = {e.scenario_id for e in report.rule_only_changes}
    assert "most-restrictive-wins" in rule_only, (
        "the block still stands via credentials-path-block, but a rule stopped matching "
        "and that must be visible"
    )


def test_the_diff_never_enforces(
    suites: list[Any], policy_document: dict[str, Any], target: OfflineTarget
) -> None:
    """Change-impact analysis must not be capable of causing the change it measures."""
    report = diff_targets(suites, target, OfflineTarget(load_bundle(policy_document)))

    assert all(e.baseline.dry_run and e.candidate.dry_run for e in report.entries)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def test_junit_xml_is_parseable_and_counts_agree(suites: list[Any], target: OfflineTarget) -> None:
    report = run_suites(suites, target)
    root = ET.fromstring(render_junit(report))

    assert root.tag == "testsuites"
    assert int(root.attrib["tests"]) == report.total
    assert int(root.attrib["failures"]) == 0
    assert len(root.findall(".//testcase")) == report.total


def test_junit_records_a_failure_element_with_the_reason(
    suites: list[Any], policy_document: dict[str, Any]
) -> None:
    broken = copy.deepcopy(policy_document)
    broken["rules"] = [r for r in broken["rules"] if r["id"] != "db-bulk-delete"]

    report = run_suites(suites, OfflineTarget(load_bundle(broken)))
    root = ET.fromstring(render_junit(report))

    failures = root.findall(".//failure")
    assert failures
    assert any("CRITICAL" in (f.attrib.get("message") or "") for f in failures)


def test_junit_records_which_target_and_mode_produced_the_result(
    suites: list[Any], target: OfflineTarget
) -> None:
    """An archived report that cannot say what it ran against proves nothing."""
    root = ET.fromstring(render_junit(run_suites(suites, target)))

    names = {p.attrib["name"] for p in root.findall(".//property")}
    assert {"target", "mode"} <= names


def test_html_report_is_self_contained(suites: list[Any], target: OfflineTarget) -> None:
    """Evidence that needs a network to render is not evidence -- it is a blank page at
    the moment someone tries to check a claim.

    Checks for actual *resource loads*, not for the substring "http". A live report
    legitimately prints its target URL in the body, so a naive substring assertion would
    either fail on every live run or be silently relaxed until it proved nothing.
    """
    import re

    html = render_html(run_suites(suites, target))

    assert "<!doctype html>" in html.lower()
    assert "<script" not in html.lower()
    assert "@import" not in html.lower()

    # Any src=/href= that leaves the document is a dependency on someone else's server.
    external = re.findall(r"""(?:src|href)\s*=\s*["']([^"']+)["']""", html, re.I)
    remote = [url for url in external if url.startswith(("http://", "https://", "//"))]
    assert not remote, f"report loads external resources: {remote}"


def test_html_report_of_a_live_run_is_still_self_contained() -> None:
    """The live path prints an https target URL into the page, which is exactly the case
    a substring check would get wrong."""
    import re

    from guardrail_sim.runner import RunReport, ScenarioResult

    scenario = Scenario.model_validate(
        {
            "id": "case",
            "action": {"tool": "db.delete_records", "arguments": {"count": 5}},
            "expect": {"decision": "allow"},
        }
    )
    report = RunReport(
        results=[
            ScenarioResult(
                scenario=scenario,
                suite_name="live suite",
                observed=ObservedDecision(decision="allow", allowed=True),
                failures=[],
                duration_ms=1.0,
            )
        ],
        target="live https://example.lambda-url.us-east-1.on.aws",
        mode="enforce",
        started_at="2026-08-19T00:00:00+00:00",
        duration_s=1.0,
    )

    html = render_html(report)

    assert "lambda-url" in html, "the report must name what it ran against"
    external = re.findall(r"""(?:src|href)\s*=\s*["']([^"']+)["']""", html, re.I)
    assert not [u for u in external if u.startswith(("http://", "https://", "//"))]


def test_html_report_escapes_scenario_content() -> None:
    """Scenario files are untrusted input; an unescaped argument would be injection."""
    suite = load_suite(
        """
        apiVersion: guardrail/v1
        name: escaping
        scenarios:
          - id: injection-attempt
            description: "<img src=x onerror=alert(1)>"
            action:
              tool: file.read
              arguments: {path: "</td><script>alert(1)</script>"}
            expect: {decision: allow}
        """,
        source="test",
    )
    bundle = load_bundle(
        {"apiVersion": "guardrail/v1", "metadata": {"bundle_id": "b"}, "rules": []}
    )

    html = render_html(run_suites([suite], OfflineTarget(bundle)))

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_json_report_carries_the_verdict(suites: list[Any], target: OfflineTarget) -> None:
    import json

    payload = json.loads(render_json(run_suites(suites, target)))

    assert payload["ok"] is True
    assert payload["totals"]["scenarios"] == len(payload["results"])
    assert all("observed" in r for r in payload["results"])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_run_exits_zero_on_a_green_suite() -> None:
    from guardrail_sim.cli import main

    assert main(["run", str(SCENARIO_DIR), "--policy", str(POLICY_FILE)]) == 0


def test_cli_run_exits_non_zero_on_a_regression(
    tmp_path: Path, policy_document: dict[str, Any]
) -> None:
    from guardrail_sim.cli import main

    broken = copy.deepcopy(policy_document)
    broken["rules"] = [r for r in broken["rules"] if r["id"] != "db-bulk-delete"]
    policy = tmp_path / "broken.yaml"
    policy.write_text(yaml.safe_dump(broken), encoding="utf-8")

    assert main(["run", str(SCENARIO_DIR), "--policy", str(policy)]) == 1


def test_cli_validate_catches_a_rule_id_that_does_not_exist(tmp_path: Path) -> None:
    """A typo in `rules_absent` can never match, so it would pass forever. Only this
    check finds a dead assertion masquerading as a control."""
    from guardrail_sim.cli import main

    (tmp_path / "typo.yaml").write_text(
        """
apiVersion: guardrail/v1
name: typo suite
scenarios:
  - id: typo-in-exclusion
    action: {tool: file.read, arguments: {path: /tmp/x}}
    expect:
      decision: allow
      rules_absent: [db-bulk-deltee]
""",
        encoding="utf-8",
    )

    assert main(["validate", str(tmp_path), "--policy", str(POLICY_FILE)]) == 1


def test_cli_reports_a_bad_path_without_a_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    from guardrail_sim.cli import main

    assert main(["run", "no/such/directory"]) == 2
    assert "ScenarioError" in capsys.readouterr().err


def test_cli_writes_the_evidence_artifacts(tmp_path: Path) -> None:
    from guardrail_sim.cli import main

    junit = tmp_path / "out" / "junit.xml"
    html = tmp_path / "out" / "report.html"

    code = main(
        [
            "run",
            str(SCENARIO_DIR),
            "--policy",
            str(POLICY_FILE),
            "--junit",
            str(junit),
            "--html",
            str(html),
        ]
    )

    assert code == 0
    assert junit.is_file() and html.is_file()
    assert ET.fromstring(junit.read_text(encoding="utf-8")).tag == "testsuites"


# ---------------------------------------------------------------------------
# The policy that is tested must be the policy that ships
# ---------------------------------------------------------------------------

PACKAGED_POLICY = (
    REPO_ROOT
    / "packages"
    / "guardrail-service"
    / "src"
    / "guardrail_service"
    / "policies"
    / "default.yaml"
)


def test_the_shipped_policy_is_the_policy_the_suite_tests() -> None:
    """Closes a silent-divergence gap.

    CI validates `policies/default.yaml` at the repo root, but the Lambda bundles
    `packages/guardrail-service/.../policies/default.yaml`. Nothing connected the two, so
    editing one and not the other would leave the conformance suite green while the
    deployed policy said something different -- a green report about a file nobody runs.

    Byte-for-byte rather than semantic: they are copies of one document, and a diff here
    means somebody edited one and forgot the other.
    """
    assert PACKAGED_POLICY.is_file(), f"no packaged bundle at {PACKAGED_POLICY}"

    root_text = POLICY_FILE.read_text(encoding="utf-8")
    packaged_text = PACKAGED_POLICY.read_text(encoding="utf-8")

    assert root_text == packaged_text, (
        "policies/default.yaml and the bundle packaged into the Lambda have diverged. "
        "The conformance suite tests the former and the deployed service runs the "
        "latter, so this difference would be invisible until production behaved "
        f"unexpectedly. Copy one over the other:\n"
        f"  cp {POLICY_FILE} {PACKAGED_POLICY}"
    )


def test_the_shipped_policy_passes_the_conformance_suite(suites: list[Any]) -> None:
    """The packaged bundle is what actually governs, so run the suite against *it*,
    not only against the copy CI happens to point at."""
    bundle = load_bundle_yaml(PACKAGED_POLICY.read_text(encoding="utf-8"))

    report = run_suites(suites, OfflineTarget(bundle, label="packaged bundle"))

    assert report.ok, [(r.scenario.id, r.failures) for r in report.results if not r.passed]
