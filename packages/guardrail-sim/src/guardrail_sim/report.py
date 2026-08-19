"""Turning a run into evidence.

Three renderings of the same `RunReport`, for three different readers:

* **console** -- the engineer running it, who wants the failures and nothing else
* **JUnit XML** -- CI, which needs a machine-readable verdict to gate a pipeline
* **HTML** -- the reviewer or auditor asking "show me this policy does what you say"

The HTML is deliberately self-contained: no CDN, no external stylesheet, no fonts. An
evidence artifact that renders differently in six months, or not at all offline, is not
evidence. It is also why the report states the *target* and the *bundle version* it ran
against -- a green report that cannot say which policy produced it proves nothing.
"""

from __future__ import annotations

import html
import json
from typing import Any
from xml.etree import ElementTree as ET

from guardrail_sim.runner import ParityReport, RunReport, ScenarioResult

# ---------------------------------------------------------------------------
# Console
# ---------------------------------------------------------------------------

_TICK = "PASS"
_CROSS = "FAIL"
_BANG = "ERROR"


def render_console(report: RunReport, *, verbose: bool = False) -> str:
    """A terminal summary. Failures always shown; passes only when asked for."""
    lines: list[str] = [
        "",
        f"Guardrail conformance -- {report.mode} mode",
        f"  target   : {report.target}",
        f"  started  : {report.started_at}",
        "",
    ]

    current_suite = ""
    for result in report.results:
        if result.suite_name != current_suite:
            current_suite = result.suite_name
            lines.append(f"  {current_suite}")

        if result.error is not None:
            lines.append(f"    {_BANG} {result.scenario.id}: {result.error}")
        elif result.failures:
            marker = f"{_CROSS}*" if result.scenario.critical else _CROSS
            lines.append(f"    {marker} {result.scenario.id}")
            for failure in result.failures:
                lines.append(f"           - {failure}")
        elif verbose:
            observed = result.observed
            detail = f" -> {observed.decision}" if observed else ""
            lines.append(f"    {_TICK} {result.scenario.id}{detail}")

    lines.extend(
        [
            "",
            f"  {report.passed}/{report.total} passed"
            f"  ({report.failed} failed, {report.errored} errored)"
            f"  in {report.duration_s}s",
        ]
    )

    if report.critical_failures:
        # Called out separately because these encode stated success criteria. A summary
        # line reading "13/15 passed" reads like a good day right up until you learn
        # which two.
        ids = ", ".join(r.scenario.id for r in report.critical_failures)
        lines.append(f"  CRITICAL: {len(report.critical_failures)} stated criteria failed: {ids}")

    lines.append("")
    return "\n".join(lines)


def render_parity_console(parity: ParityReport) -> str:
    """Summary of a dry-run parity check."""
    lines = [
        "",
        "Dry-run parity",
        f"  target : {parity.enforce.target}",
        f"  scenarios: {parity.enforce.total}",
        "",
    ]

    if parity.findings:
        lines.append("  Dry-run reported a different decision than enforcement:")
        for finding in parity.findings:
            lines.append(
                f"    FAIL {finding.scenario_id}: enforce={finding.enforce_decision} "
                f"dry_run={finding.dry_run_decision}"
            )
    else:
        lines.append(
            f"  PASS every one of {parity.enforce.total} scenarios reported an identical "
            "decision in dry-run and enforcement, and no tool was executed."
        )

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JUnit XML
# ---------------------------------------------------------------------------


def render_junit(report: RunReport) -> str:
    """JUnit XML, the format every CI system already understands.

    A scenario failure becomes `<failure>` and a target problem becomes `<error>`,
    matching the distinction the runner draws: one means the policy is wrong, the other
    means the run was invalid. CI dashboards render them differently, which is exactly
    the signal a reader needs.
    """
    root = ET.Element(
        "testsuites",
        {
            "name": "guardrail-conformance",
            "tests": str(report.total),
            "failures": str(report.failed),
            "errors": str(report.errored),
            "time": str(report.duration_s),
        },
    )

    for suite_name in _suite_order(report):
        results = [r for r in report.results if r.suite_name == suite_name]
        suite_el = ET.SubElement(
            root,
            "testsuite",
            {
                "name": suite_name,
                "tests": str(len(results)),
                "failures": str(sum(1 for r in results if r.failures and r.error is None)),
                "errors": str(sum(1 for r in results if r.error is not None)),
                "time": str(round(sum(r.duration_ms for r in results) / 1000, 3)),
                "timestamp": report.started_at,
            },
        )
        # Properties travel with the results, so an archived XML still answers "which
        # endpoint and which policy version produced this?" months later.
        properties = ET.SubElement(suite_el, "properties")
        for name, value in (("target", report.target), ("mode", report.mode)):
            ET.SubElement(properties, "property", {"name": name, "value": value})

        for result in results:
            _junit_case(suite_el, suite_name, result)

    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode")


def _junit_case(suite_el: ET.Element, suite_name: str, result: ScenarioResult) -> None:
    case = ET.SubElement(
        suite_el,
        "testcase",
        {
            "classname": suite_name,
            "name": result.scenario.id,
            "time": str(round(result.duration_ms / 1000, 4)),
        },
    )

    if result.error is not None:
        error = ET.SubElement(case, "error", {"message": result.error, "type": "TargetError"})
        error.text = result.error
        return

    if result.failures:
        prefix = "CRITICAL: " if result.scenario.critical else ""
        failure = ET.SubElement(
            case,
            "failure",
            {"message": prefix + result.failures[0], "type": "PolicyExpectationFailed"},
        )
        failure.text = "\n".join(result.failures)

    if result.observed is not None:
        out = ET.SubElement(case, "system-out")
        out.text = json.dumps(_observed_dict(result), indent=2)


def _observed_dict(result: ScenarioResult) -> dict[str, Any]:
    observed = result.observed
    if observed is None:
        return {}
    return {
        "decision": observed.decision,
        "allowed": observed.allowed,
        "matched_rules": observed.rule_ids,
        "message": observed.message,
        "unknown_paths": observed.unknown_paths,
        "bundle": f"{observed.bundle_id} v{observed.bundle_version}",
        "dry_run": observed.dry_run,
        "engine_latency_ms": observed.latency_ms,
    }


def _suite_order(report: RunReport) -> list[str]:
    """Suite names in first-seen order, so the report reads in file order."""
    seen: list[str] = []
    for result in report.results:
        if result.suite_name not in seen:
            seen.append(result.suite_name)
    return seen


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_CSS = """
:root { color-scheme: light dark; --fg:#16181d; --bg:#ffffff; --muted:#5b6472;
        --line:#e3e6ea; --pass:#0f7b3f; --fail:#b4232c; --err:#8a5a00;
        --chip:#f2f4f7; --card:#fbfcfd; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e8eaed; --bg:#101317; --muted:#9aa4b2; --line:#262b33;
          --pass:#4cc38a; --fail:#ff6b6b; --err:#e0b341; --chip:#1a1f26; --card:#161a20; }
}
* { box-sizing: border-box; }
body { margin:0; padding:2rem 1.25rem 4rem; background:var(--bg); color:var(--fg);
       font:15px/1.55 ui-sans-serif,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }
main { max-width: 68rem; margin: 0 auto; }
h1 { font-size:1.5rem; margin:0 0 .35rem; letter-spacing:-.01em; }
.sub { color:var(--muted); margin:0 0 1.5rem; font-size:.92rem; }
.meta { display:flex; flex-wrap:wrap; gap:.5rem; margin-bottom:1.5rem; }
.chip { background:var(--chip); border:1px solid var(--line); border-radius:999px;
        padding:.25rem .7rem; font-size:.8rem; color:var(--muted); }
.chip b { color:var(--fg); font-weight:600; }
.totals { display:flex; gap:1.5rem; padding:1rem 1.25rem; border:1px solid var(--line);
          border-radius:10px; background:var(--card); margin-bottom:1.75rem; flex-wrap:wrap; }
.totals div { min-width:5rem; }
.totals span { display:block; font-size:1.55rem; font-weight:650; letter-spacing:-.02em; }
.totals small { color:var(--muted); font-size:.78rem; text-transform:uppercase;
                letter-spacing:.06em; }
.banner { padding:.85rem 1.1rem; border-radius:10px; margin-bottom:1.75rem;
          font-weight:600; border:1px solid; }
.banner.ok { color:var(--pass); border-color:var(--pass); }
.banner.bad { color:var(--fail); border-color:var(--fail); }
h2 { font-size:1rem; margin:2rem 0 .6rem; text-transform:uppercase; letter-spacing:.07em;
     color:var(--muted); font-weight:600; }
table { width:100%; border-collapse:collapse; font-size:.9rem; }
th { text-align:left; font-weight:600; color:var(--muted); font-size:.78rem;
     text-transform:uppercase; letter-spacing:.05em; padding:.5rem .6rem;
     border-bottom:1px solid var(--line); }
td { padding:.6rem; border-bottom:1px solid var(--line); vertical-align:top; }
tr.fail td { background:color-mix(in srgb, var(--fail) 7%, transparent); }
.status { font-weight:700; font-size:.78rem; letter-spacing:.04em; }
.status.pass { color:var(--pass); } .status.fail { color:var(--fail); }
.status.error { color:var(--err); }
code { font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
       background:var(--chip); padding:.1rem .35rem; border-radius:4px; }
.why { margin:.4rem 0 0; padding-left:1.1rem; color:var(--fail); font-size:.85rem; }
.crit { display:inline-block; font-size:.65rem; font-weight:700; letter-spacing:.06em;
        border:1px solid var(--err); color:var(--err); border-radius:4px;
        padding:.05rem .3rem; margin-left:.4rem; vertical-align:middle; }
.desc { color:var(--muted); font-size:.82rem; display:block; margin-top:.15rem; }
footer { margin-top:3rem; color:var(--muted); font-size:.8rem; border-top:1px solid var(--line);
         padding-top:1rem; }
"""


def render_html(report: RunReport) -> str:
    """A self-contained evidence page.

    Inline CSS and no scripts on purpose: this file gets attached to a ticket, emailed,
    and opened from disk with no network. Anything that needs fetching would render as a
    blank page precisely when someone is trying to check a claim.
    """
    esc = html.escape
    status_class = "ok" if report.ok else "bad"
    headline = (
        f"All {report.total} scenarios behaved as the policy promises."
        if report.ok
        else f"{report.failed + report.errored} of {report.total} scenarios did not."
    )

    rows: list[str] = []
    for suite_name in _suite_order(report):
        rows.append(
            f'<h2>{esc(suite_name)}</h2><table><thead><tr><th style="width:30%">Scenario'
            "</th><th>Action</th><th>Expected</th><th>Observed</th>"
            '<th style="width:8%">Result</th></tr></thead><tbody>'
        )
        for result in (r for r in report.results if r.suite_name == suite_name):
            rows.append(_html_row(result))
        rows.append("</tbody></table>")

    critical_note = ""
    if report.critical_failures:
        ids = ", ".join(esc(r.scenario.id) for r in report.critical_failures)
        critical_note = (
            f'<div class="banner bad">{len(report.critical_failures)} stated success '
            f"criteria failed: {ids}</div>"
        )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Guardrail conformance report</title>
<style>{_CSS}</style></head><body><main>
<h1>Guardrail conformance report</h1>
<p class="sub">Every scenario asks the deployed policy what it would do about one tool
call, and checks the answer. No tool is executed.</p>
<div class="meta">
  <span class="chip">target <b>{esc(report.target)}</b></span>
  <span class="chip">mode <b>{esc(report.mode)}</b></span>
  <span class="chip">started <b>{esc(report.started_at)}</b></span>
  <span class="chip">duration <b>{report.duration_s}s</b></span>
</div>
<div class="banner {status_class}">{esc(headline)}</div>
{critical_note}
<div class="totals">
  <div><span>{report.total}</span><small>scenarios</small></div>
  <div><span style="color:var(--pass)">{report.passed}</span><small>passed</small></div>
  <div><span style="color:var(--fail)">{report.failed}</span><small>failed</small></div>
  <div><span style="color:var(--err)">{report.errored}</span><small>errored</small></div>
</div>
{"".join(rows)}
<footer>Generated by <code>guardrail-sim</code>. The report names the target and the policy
bundle version behind every decision, so it can be re-derived rather than trusted.</footer>
</main></body></html>
"""


def _html_row(result: ScenarioResult) -> str:
    esc = html.escape
    scenario = result.scenario
    observed = result.observed

    if result.error is not None:
        status, css = "ERROR", "error"
    elif result.failures:
        status, css = "FAIL", "fail"
    else:
        status, css = "PASS", "pass"

    critical = '<span class="crit">CRITICAL</span>' if scenario.critical else ""
    description = (
        f'<span class="desc">{esc(scenario.description)}</span>' if scenario.description else ""
    )

    expected_bits: list[str] = []
    if scenario.expect.decision:
        expected_bits.append(f"<code>{esc(scenario.expect.decision)}</code>")
    if scenario.expect.allowed is not None:
        expected_bits.append(f"allowed={scenario.expect.allowed}")
    if scenario.expect.rules:
        expected_bits.append(
            "via " + ", ".join(f"<code>{esc(r)}</code>" for r in scenario.expect.rules)
        )
    if scenario.expect.rules_absent:
        expected_bits.append(
            "not " + ", ".join(f"<code>{esc(r)}</code>" for r in scenario.expect.rules_absent)
        )
    if scenario.expect.message_contains:
        expected_bits.append(f"message ~ {esc(scenario.expect.message_contains)!r}")
    if scenario.expect.unknown_paths:
        expected_bits.append(
            "unknown " + ", ".join(f"<code>{esc(p)}</code>" for p in scenario.expect.unknown_paths)
        )

    if result.error is not None:
        observed_html = f'<span style="color:var(--err)">{esc(result.error)}</span>'
    elif observed is not None:
        rules = ", ".join(f"<code>{esc(r)}</code>" for r in observed.rule_ids) or "&mdash;"
        observed_html = f"<code>{esc(observed.decision)}</code> via {rules}"
        if observed.unknown_paths:
            observed_html += (
                '<span class="desc">unknown: '
                + ", ".join(esc(p) for p in observed.unknown_paths)
                + "</span>"
            )
        if observed.message:
            observed_html += f'<span class="desc">{esc(observed.message[:180])}</span>'
    else:
        observed_html = "&mdash;"

    why = ""
    if result.failures:
        why = '<ul class="why">' + "".join(f"<li>{esc(f)}</li>" for f in result.failures) + "</ul>"

    args = json.dumps(scenario.action.arguments, separators=(", ", ": "))
    action_html = f"<code>{esc(scenario.action.tool)}</code>"
    if scenario.action.arguments:
        action_html += f'<span class="desc">{esc(args[:160])}</span>'

    row_class = ' class="fail"' if status != "PASS" else ""
    return (
        f"<tr{row_class}>"
        f"<td><strong>{esc(scenario.id)}</strong>{critical}{description}{why}</td>"
        f"<td>{action_html}</td>"
        f"<td>{' &middot; '.join(expected_bits)}</td>"
        f"<td>{observed_html}</td>"
        f'<td><span class="status {css}">{status}</span></td>'
        "</tr>"
    )


def render_json(report: RunReport) -> str:
    """Machine-readable results, for anything that is not a CI dashboard."""
    return json.dumps(
        {
            "target": report.target,
            "mode": report.mode,
            "started_at": report.started_at,
            "duration_s": report.duration_s,
            "totals": {
                "scenarios": report.total,
                "passed": report.passed,
                "failed": report.failed,
                "errored": report.errored,
            },
            "ok": report.ok,
            "results": [
                {
                    "id": r.scenario.id,
                    "suite": r.suite_name,
                    "critical": r.scenario.critical,
                    "passed": r.passed,
                    "failures": r.failures,
                    "error": r.error,
                    "observed": _observed_dict(r),
                }
                for r in report.results
            ],
        },
        indent=2,
    )
