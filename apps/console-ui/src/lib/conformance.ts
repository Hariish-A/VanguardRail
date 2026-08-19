/**
 * The conformance runner, client side.
 *
 * ## What this is, and what it is not
 *
 * It runs the **real** scenario corpus — `scenarios/*.yaml`, compiled into the bundle at
 * build time by `scripts/generate-scenarios.mjs` — against the **live** control plane,
 * and reports pass or fail per scenario.
 *
 * It is **not** the canonical runner. `guardrail-sim` is, and it is what gates a pull
 * request in CI: it validates the DSL far more strictly, runs offline against the policy
 * file as well as live, produces JUnit XML, and includes
 * `test_the_suite_actually_fails_when_policy_regresses`, which deletes a rule and
 * requires the suite to go red. This page is a live view of the same corpus, not a
 * replacement for that.
 *
 * ## The drift risk, named
 *
 * `checkExpectation` below re-implements `guardrail_sim.runner.check` in TypeScript.
 * Two implementations of one rule can disagree, and the failure mode is a green console
 * next to a red CI run — or worse, the reverse. Three things bound it:
 *
 * * the corpus is generated from the YAML on every build, so the *inputs* cannot drift;
 * * every assertion kind has a unit test in `conformance.test.ts`, written from the
 *   Python semantics rather than from this file;
 * * the page states which runner is canonical, so a disagreement is investigated rather
 *   than averaged.
 *
 * ## Why simulate rather than evaluate
 *
 * `/v1/simulate` writes no audit record. Running twenty scenarios from a browser tab on
 * the enforcement path would put twenty rows of speculation into the tamper-evident log
 * every time somebody opened this page, and the log is the deliverable. The trade-off is
 * that simulation does not exercise the audit write — which is exactly why the CI run
 * uses `--endpoint`, on the enforcement path, and this page says so.
 */

import corpus from "@/generated/scenarios.json";
import { api, type Session } from "./api";
import type { Effect, SimulateResponse } from "./types";

export interface ScenarioExpectation {
  decision: Effect | null;
  allowed: boolean | null;
  rules: string[];
  rules_absent: string[];
  message_contains: string | null;
  unknown_paths: string[];
}

export interface Scenario {
  id: string;
  description: string;
  critical: boolean;
  tags: string[];
  action: {
    tool: string;
    arguments: Record<string, unknown>;
    context: Record<string, unknown>;
  };
  expect: ScenarioExpectation;
}

export interface Suite {
  file: string;
  name: string;
  description: string;
  agent_id: string;
  scenarios: Scenario[];
}

export const SUITES: Suite[] = (corpus as { suites: Suite[] }).suites;

export const ALL_SCENARIOS: Array<Scenario & { suite: string }> = SUITES.flatMap((suite) =>
  suite.scenarios.map((scenario) => ({ ...scenario, suite: suite.name })),
);

/** What the server actually said, reduced to the fields expectations talk about. */
export interface Observed {
  decision: Effect;
  allowed: boolean;
  ruleIds: string[];
  message: string | null;
  unknownPaths: string[];
  derived: Record<string, unknown>;
  latencyMs: number;
  bundleVersion: number;
  bundleSource: string;
}

export function observe(response: SimulateResponse): Observed {
  return {
    decision: response.decision,
    allowed: response.allowed,
    ruleIds: response.matched_rules.map((rule) => rule.rule_id),
    message: response.message,
    unknownPaths: response.unknown_paths ?? [],
    derived: response.derived ?? {},
    latencyMs: response.latency_ms,
    bundleVersion: response.bundle_version,
    bundleSource: response.bundle_source,
  };
}

/**
 * Compare an observed decision against an expectation.
 *
 * Returns **every** failure rather than the first, matching the Python runner: fixing a
 * policy one assertion per run is a slow loop, and the full list usually shows the single
 * underlying cause.
 */
export function checkExpectation(
  expect: ScenarioExpectation,
  observed: Observed,
): string[] {
  const failures: string[] = [];

  if (expect.decision !== null && observed.decision !== expect.decision) {
    const matched = observed.ruleIds.join(", ");
    failures.push(
      `expected decision '${expect.decision}', got '${observed.decision}'` +
        (matched ? ` (matched: ${matched})` : " (no rules matched)"),
    );
  }

  if (expect.allowed !== null && observed.allowed !== expect.allowed) {
    failures.push(`expected allowed=${expect.allowed}, got allowed=${observed.allowed}`);
  }

  const missing = expect.rules.filter((rule) => !observed.ruleIds.includes(rule));
  if (missing.length > 0) {
    failures.push(
      `expected rule(s) ${missing.join(", ")} to match; matched ` +
        `${observed.ruleIds.join(", ") || "nothing"}`,
    );
  }

  const present = expect.rules_absent.filter((rule) => observed.ruleIds.includes(rule));
  if (present.length > 0) {
    failures.push(`rule(s) ${present.join(", ")} matched but should not have`);
  }

  if (expect.message_contains !== null) {
    const message = observed.message ?? "";
    if (!message.includes(expect.message_contains)) {
      failures.push(
        `expected the message to contain '${expect.message_contains}'; got ` +
          `'${message.slice(0, 200)}'`,
      );
    }
  }

  const missingUnknown = expect.unknown_paths.filter(
    (path) => !observed.unknownPaths.includes(path),
  );
  if (missingUnknown.length > 0) {
    failures.push(
      `expected path(s) ${missingUnknown.join(", ")} to be reported UNKNOWN; ` +
        `reported ${observed.unknownPaths.join(", ") || "none"}`,
    );
  }

  return failures;
}

export interface ScenarioResult {
  scenario: Scenario & { suite: string };
  observed: Observed | null;
  failures: string[];
  /** Set when the request itself failed, which is different from an assertion failing. */
  error: string | null;
  durationMs: number;
}

export function verdict(result: ScenarioResult): "pass" | "fail" | "error" {
  if (result.error !== null) return "error";
  return result.failures.length === 0 ? "pass" : "fail";
}

export interface RunSummary {
  results: ScenarioResult[];
  passed: number;
  failed: number;
  errored: number;
  criticalFailures: number;
  durationMs: number;
}

export function summarise(results: ScenarioResult[], durationMs: number): RunSummary {
  return {
    results,
    passed: results.filter((r) => verdict(r) === "pass").length,
    failed: results.filter((r) => verdict(r) === "fail").length,
    errored: results.filter((r) => verdict(r) === "error").length,
    // Surfaced separately because a summary line reading "18 of 20 passed" must not hide
    // that one of the two failures was a stated requirement of the problem statement.
    criticalFailures: results.filter((r) => verdict(r) !== "pass" && r.scenario.critical)
      .length,
    durationMs,
  };
}

/**
 * Run the corpus, reporting progress as it goes.
 *
 * Sequential on purpose. The deployed service is rate limited per tenant and provisioned
 * at 5 write units; firing twenty concurrent requests from a browser would produce 429s
 * that look like policy failures. Twenty sequential simulations take about a second.
 */
export async function runCorpus(
  session: Session,
  scenarios: Array<Scenario & { suite: string }>,
  options: {
    onResult?: (result: ScenarioResult, index: number) => void;
    version?: number;
    bundle?: Record<string, unknown>;
    signal?: AbortSignal;
  } = {},
): Promise<RunSummary> {
  const started = performance.now();
  const results: ScenarioResult[] = [];

  for (const [index, scenario] of scenarios.entries()) {
    if (options.signal?.aborted) break;

    const scenarioStarted = performance.now();
    let result: ScenarioResult;

    try {
      const response = await api.simulateAgainst(
        session,
        {
          agent_id: "console-conformance",
          session_id: `conformance-${index}`,
          tool: scenario.action.tool,
          arguments: scenario.action.arguments,
        },
        { version: options.version, bundle: options.bundle },
      );
      const observed = observe(response);
      result = {
        scenario,
        observed,
        failures: checkExpectation(scenario.expect, observed),
        error: null,
        durationMs: performance.now() - scenarioStarted,
      };
    } catch (cause) {
      // An unreachable service is not a policy failure, and reporting it as one would
      // send somebody to edit a rule that is working correctly.
      result = {
        scenario,
        observed: null,
        failures: [],
        error: cause instanceof Error ? cause.message : String(cause),
        durationMs: performance.now() - scenarioStarted,
      };
    }

    results.push(result);
    options.onResult?.(result, index);
  }

  return summarise(results, performance.now() - started);
}
