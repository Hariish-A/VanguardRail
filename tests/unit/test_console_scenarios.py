"""The console's compiled scenario corpus must match the real one.

`apps/console-ui/src/generated/scenarios.json` is generated from `scenarios/*.yaml` by a
prebuild step, and the console's Conformance page runs it against live AWS. That page
reports pass/fail in green, which makes drift here worse than an ordinary bug: a
conformance report built from a stale corpus reports success against scenarios nobody
enforces any more.

The generator runs on every `npm run build`, so drift cannot survive a build. What it
*can* survive is a checked-in generated file that someone edited by hand, or a corpus
regenerated before a scenario was added. This test closes both.

It is skipped when the generated file is absent — a clean checkout that has never run the
frontend build is a normal state, not a failure. `test_the_generated_corpus_is_present_in_ci`
makes sure that skip cannot hide a missing file where it matters.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCENARIO_DIR = REPO / "scenarios"
GENERATED = REPO / "apps" / "console-ui" / "src" / "generated" / "scenarios.json"


def _yaml_scenarios() -> dict[str, dict[str, Any]]:
    """Every enabled scenario in the canonical files, keyed by id."""
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(SCENARIO_DIR.glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for scenario in document.get("scenarios", []):
            if scenario.get("enabled", True):
                found[scenario["id"]] = scenario
    return found


def _generated() -> dict[str, dict[str, Any]]:
    payload = json.loads(GENERATED.read_text(encoding="utf-8"))
    return {
        scenario["id"]: scenario for suite in payload["suites"] for scenario in suite["scenarios"]
    }


requires_generated = pytest.mark.skipif(
    not GENERATED.exists(),
    reason="console corpus not generated; run `npm run scenarios` in apps/console-ui",
)


@requires_generated
def test_the_console_corpus_has_the_same_scenarios() -> None:
    """Same ids, no more and no fewer.

    A scenario present in the YAML and missing from the console is silently untested
    there; one present in the console and missing from the YAML is a scenario nobody
    maintains, still being reported as evidence.
    """
    from_yaml = set(_yaml_scenarios())
    from_console = set(_generated())

    assert from_console == from_yaml, (
        f"missing from the console: {sorted(from_yaml - from_console)}; "
        f"stale in the console: {sorted(from_console - from_yaml)}. "
        "Run `npm run scenarios` in apps/console-ui."
    )


@requires_generated
def test_the_expectations_were_compiled_faithfully() -> None:
    """The console must assert exactly what the canonical suite asserts.

    Weakening one expectation during compilation -- dropping a `rules` list, say -- would
    leave the console reporting green on a scenario CI reports red.
    """
    from_yaml = _yaml_scenarios()
    from_console = _generated()

    mismatches: list[str] = []
    for scenario_id, source in from_yaml.items():
        expected = source["expect"]
        compiled = from_console[scenario_id]["expect"]

        for field, default in (
            ("decision", None),
            ("allowed", None),
            ("rules", []),
            ("rules_absent", []),
            ("message_contains", None),
            ("unknown_paths", []),
        ):
            if expected.get(field, default) != compiled.get(field, default):
                mismatches.append(
                    f"{scenario_id}.{field}: yaml={expected.get(field, default)!r} "
                    f"console={compiled.get(field, default)!r}"
                )

        if source["action"]["tool"] != from_console[scenario_id]["action"]["tool"]:
            mismatches.append(f"{scenario_id}.tool differs")
        if (
            source["action"].get("arguments", {})
            != from_console[scenario_id]["action"]["arguments"]
        ):
            mismatches.append(f"{scenario_id}.arguments differ")

    assert not mismatches, "\n".join(mismatches)


@requires_generated
def test_critical_scenarios_stay_critical() -> None:
    """`critical` is what stops "18 of 20 passed" hiding a failed requirement. Losing the
    flag in compilation would quietly demote a stated success criterion."""
    from_yaml = _yaml_scenarios()
    from_console = _generated()

    for scenario_id, source in from_yaml.items():
        assert bool(source.get("critical", False)) == bool(from_console[scenario_id]["critical"]), (
            f"{scenario_id}: critical flag differs"
        )


@requires_generated
def test_the_corpus_is_not_empty() -> None:
    """An empty corpus makes the console report "0 of 0 passed" in green, which is the
    most misleading thing a test report can say."""
    assert len(_generated()) >= 20


def test_the_generated_corpus_is_present_in_ci() -> None:
    """The skip above is right locally and wrong in CI.

    Without this, a build that never generated the corpus would skip every check in this
    file and report green -- the vacuous-pass failure mode this repository has hit
    repeatedly. In CI the console job runs `npm ci && npm run build`, which generates it,
    so its absence there is a real failure.
    """
    if not os.environ.get("CI"):
        pytest.skip("only meaningful in CI, where the console is always built")

    assert GENERATED.exists(), (
        f"{GENERATED} is missing in CI. The console's conformance page would ship with "
        "no scenarios, and every check in this file would skip rather than fail."
    )
