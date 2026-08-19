"""`guardrail-sim` -- the command line.

Four verbs, each answering a different question an operator actually asks:

    guardrail-sim validate scenarios/                 is the suite itself sound?
    guardrail-sim run      scenarios/ --endpoint URL  does the live policy behave?
    guardrail-sim parity   scenarios/ --endpoint URL  does dry-run tell the truth?
    guardrail-sim diff     scenarios/ --candidate F   what would this edit change?

`run` returns a non-zero exit code on any failure, which is what makes it usable as a
post-deploy CI gate rather than a report someone remembers to read.

Every command works **offline** as well as against a deployed endpoint. That is not a
convenience: it is what lets the conformance suite gate a pull request, before there is
anything deployed to point at.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from guardrail_sim.diffing import SimulateTarget, diff_targets, render_diff_console
from guardrail_sim.report import (
    render_console,
    render_html,
    render_json,
    render_junit,
    render_parity_console,
)
from guardrail_sim.runner import (
    LiveTarget,
    OfflineTarget,
    Target,
    TargetError,
    run_parity,
    run_suites,
)
from guardrail_sim.scenarios import ScenarioError, ScenarioSuite, load_suites

DEFAULT_POLICY = "policies/default.yaml"


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _load_policy(path: str) -> Any:
    from guardrail_core.policy import load_bundle_yaml

    file = Path(path)
    if not file.is_file():
        raise ScenarioError(f"no policy bundle at {path}")
    return load_bundle_yaml(file.read_text(encoding="utf-8"))


def _build_target(args: argparse.Namespace) -> Target:
    """Choose a live or offline target from the flags.

    An explicit `--endpoint` always wins. Falling back to `GUARDRAIL_BASE_URL` silently
    would be worse than useless here: someone running an offline check would think they
    had, while actually writing audit records into a deployed tenant.
    """
    if args.endpoint:
        return LiveTarget(args.endpoint, args.api_key or os.environ.get("GUARDRAIL_API_KEY", ""))
    return OfflineTarget(_load_policy(args.policy), label=f"offline {args.policy}")


def _write(path: str | None, content: str, label: str) -> None:
    if not path:
        return
    out = Path(path)
    if out.parent != Path():
        out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
    print(f"  wrote {label}: {out}")


def _target_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("paths", nargs="+", help="Scenario files or directories.")
    parser.add_argument(
        "--endpoint",
        default="",
        help="Base URL of a deployed control plane. Omit to evaluate offline.",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="API key for the endpoint. Falls back to GUARDRAIL_API_KEY.",
    )
    parser.add_argument(
        "--policy",
        default=DEFAULT_POLICY,
        help=f"Policy bundle for offline evaluation (default: {DEFAULT_POLICY}).",
    )


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
    """Check the scenario files, and check them *against a policy*.

    The second half is the part that earns its keep. A scenario naming a rule id that no
    longer exists still fails loudly when it appears in `expect.rules` -- but a typo in
    `expect.rules_absent` passes forever, because an id that does not exist can never
    match. That is a dead assertion masquerading as a control, and only this check finds
    it.
    """
    suites = load_suites(args.paths)
    bundle = _load_policy(args.policy)
    known = {rule.id for rule in bundle.rules}

    problems: list[str] = []
    total = 0
    for suite in suites:
        for scenario in suite.scenarios:
            total += 1
            for rule_id in [*scenario.expect.rules, *scenario.expect.rules_absent]:
                if rule_id not in known:
                    problems.append(
                        f"{suite.source}: scenario {scenario.id!r} references rule "
                        f"{rule_id!r}, which is not in {args.policy}"
                    )

    print(f"\n  {len(suites)} suite(s), {total} scenario(s) parsed against {args.policy}")
    if problems:
        print("\n  Problems:")
        for problem in problems:
            print(f"    - {problem}")
        print("")
        return 1

    print("  All scenario files are valid and every referenced rule id exists.\n")
    return 0


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    suites: list[ScenarioSuite] = load_suites(args.paths)
    target = _build_target(args)

    report = run_suites(suites, target, dry_run=args.dry_run)

    print(render_console(report, verbose=args.verbose))
    _write(args.junit, render_junit(report), "JUnit XML")
    _write(args.html, render_html(report), "HTML report")
    _write(args.json, render_json(report), "JSON results")

    return report.exit_code


# ---------------------------------------------------------------------------
# parity
# ---------------------------------------------------------------------------


def cmd_parity(args: argparse.Namespace) -> int:
    """Prove dry-run reports what enforcement would really do.

    Without this, "shadow mode says your change is safe" is an unverified claim -- and
    it is a claim people make deployment decisions on.
    """
    suites = load_suites(args.paths)
    target = _build_target(args)

    parity = run_parity(suites, target)
    print(render_parity_console(parity))

    if not parity.enforce.ok:
        print(render_console(parity.enforce))

    _write(args.junit, render_junit(parity.enforce), "JUnit XML")
    _write(args.html, render_html(parity.dry), "HTML report (dry-run pass)")

    return parity.exit_code


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def cmd_diff(args: argparse.Namespace) -> int:
    """Compare a candidate policy against the baseline over the scenario corpus."""
    import yaml

    suites = load_suites(args.paths)

    if args.endpoint:
        api_key = args.api_key or os.environ.get("GUARDRAIL_API_KEY", "")
        baseline: Target = SimulateTarget(args.endpoint, api_key)
        if args.candidate_version is not None:
            candidate: Target = SimulateTarget(
                args.endpoint, api_key, version=args.candidate_version
            )
        elif args.candidate:
            raw = yaml.safe_load(Path(args.candidate).read_text(encoding="utf-8"))
            candidate = SimulateTarget(args.endpoint, api_key, bundle=raw)
        else:
            raise ScenarioError("pass --candidate FILE or --candidate-version N")
    else:
        if not args.candidate:
            raise ScenarioError("offline diff needs --candidate FILE")
        baseline_path = args.baseline or args.policy
        baseline = OfflineTarget(_load_policy(baseline_path), label=baseline_path)
        candidate = OfflineTarget(_load_policy(args.candidate), label=args.candidate)

    report = diff_targets(suites, baseline, candidate)
    print(render_diff_console(report))

    # Deliberately exit 0 even when decisions change. A diff is information, not a
    # verdict -- changing behaviour is the entire point of publishing a new policy. Use
    # --fail-on-change in a pipeline that wants a human to acknowledge the change first.
    if args.fail_on_change and report.changed:
        print(f"  --fail-on-change set and {len(report.changed)} decision(s) would change.\n")
        return 1
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="guardrail-sim",
        description="Prove a Guardrail policy behaves as intended -- offline or against a "
        "deployed control plane. No tool is ever executed.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="Check scenario files against a policy bundle.")
    validate.add_argument("paths", nargs="+")
    validate.add_argument("--policy", default=DEFAULT_POLICY)
    validate.set_defaults(func=cmd_validate)

    run = sub.add_parser("run", help="Run the conformance suite. Non-zero exit on failure.")
    _target_flags(run)
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Send every action with dry_run=true. Decisions are recorded and reported; "
        "nothing is enforced and no tool runs.",
    )
    run.add_argument("--junit", default="", help="Write JUnit XML here.")
    run.add_argument("--html", default="", help="Write a self-contained HTML report here.")
    run.add_argument("--json", default="", help="Write machine-readable results here.")
    run.add_argument("-v", "--verbose", action="store_true", help="List passing scenarios too.")
    run.set_defaults(func=cmd_run)

    parity = sub.add_parser(
        "parity",
        help="Run every scenario in both enforce and dry-run mode and prove they agree.",
    )
    _target_flags(parity)
    parity.add_argument("--junit", default="")
    parity.add_argument("--html", default="")
    parity.set_defaults(func=cmd_parity)

    diff = sub.add_parser("diff", help="Change-impact analysis for a candidate policy.")
    _target_flags(diff)
    diff.add_argument("--candidate", default="", help="Candidate policy bundle file.")
    diff.add_argument(
        "--candidate-version",
        type=int,
        default=None,
        help="Published bundle version to compare against (requires --endpoint).",
    )
    diff.add_argument(
        "--baseline",
        default="",
        help="Baseline policy file for an offline diff (default: --policy).",
    )
    diff.add_argument(
        "--fail-on-change",
        action="store_true",
        help="Exit non-zero if any decision would change. For a pipeline that requires "
        "a human to acknowledge policy drift.",
    )
    diff.set_defaults(func=cmd_diff)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        result: int = args.func(args)
    except (ScenarioError, TargetError) as exc:
        # A malformed suite or an unreachable endpoint is an operator error, not a
        # crash. Print the sentence, not a traceback.
        print(f"\n  {type(exc).__name__}: {exc}\n", file=sys.stderr)
        return 2

    return result


if __name__ == "__main__":  # pragma: no cover - exercised via `python -m guardrail_sim`
    raise SystemExit(main())
