"""Guardrail simulation harness.

Runs scenario files against a policy -- offline against a bundle on disk, or live
against a deployed control plane -- and emits console, JUnit XML, HTML, and JSON
evidence.

    guardrail-sim run scenarios/ --endpoint https://... --junit out.xml --html out.html

The harness asks what policy *would* decide. It never executes a tool, which is what
makes it safe to point at production and honest about what it proves.
"""

from guardrail_sim.diffing import DiffEntry, DiffReport, SimulateTarget, diff_targets
from guardrail_sim.report import render_console, render_html, render_json, render_junit
from guardrail_sim.runner import (
    LiveTarget,
    ObservedDecision,
    OfflineTarget,
    ParityReport,
    RunReport,
    ScenarioResult,
    TargetError,
    build_action,
    check,
    run_parity,
    run_suites,
)
from guardrail_sim.scenarios import (
    Expectation,
    Scenario,
    ScenarioError,
    ScenarioSuite,
    load_suite,
    load_suites,
)

__all__ = [
    "DiffEntry",
    "DiffReport",
    "Expectation",
    "LiveTarget",
    "ObservedDecision",
    "OfflineTarget",
    "ParityReport",
    "RunReport",
    "Scenario",
    "ScenarioError",
    "ScenarioResult",
    "ScenarioSuite",
    "SimulateTarget",
    "TargetError",
    "build_action",
    "check",
    "diff_targets",
    "load_suite",
    "load_suites",
    "render_console",
    "render_html",
    "render_json",
    "render_junit",
    "run_parity",
    "run_suites",
]

__version__ = "0.1.0"
