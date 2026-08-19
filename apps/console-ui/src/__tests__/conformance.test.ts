/**
 * The conformance checker and the change-impact comparison.
 *
 * These two functions decide whether the console reports green or red, and whether a
 * policy change is called *looser*. Both are re-implementations of logic that already
 * exists in Python — `guardrail_sim.runner.check` and the engine's effect ordering — so
 * the risk here is not a crash. It is a quiet disagreement: a console showing green next
 * to a red CI run, or worse, the reverse.
 *
 * So these tests are written from the **Python semantics**, not from the TypeScript. Each
 * assertion kind gets a case, including the ones that are easy to get backwards.
 */

import { describe, expect, it } from "vitest";
import {
  ALL_SCENARIOS,
  checkExpectation,
  observe,
  summarise,
  SUITES,
  verdict,
  type Observed,
  type ScenarioResult,
} from "@/lib/conformance";
import { compareDecisions, parseBundle, rulesOf, toYaml } from "@/lib/policy";
import type { Effect, SimulateResponse } from "@/lib/types";

function observed(overrides: Partial<Observed> = {}): Observed {
  return {
    decision: "block",
    allowed: false,
    ruleIds: ["db-bulk-delete"],
    message: "Bulk delete of 500 records exceeds the limit of 100.",
    unknownPaths: [],
    derived: { record_count: 500 },
    latencyMs: 4.2,
    bundleVersion: 1,
    bundleSource: "active",
    ...overrides,
  };
}

const NOTHING_EXPECTED = {
  decision: null,
  allowed: null,
  rules: [],
  rules_absent: [],
  message_contains: null,
  unknown_paths: [],
};

// ---------------------------------------------------------------------------
// The corpus itself
// ---------------------------------------------------------------------------

describe("the compiled corpus", () => {
  it("is not empty, because an empty suite reports 0/0 in green", () => {
    // The most misleading thing a test report can say.
    expect(ALL_SCENARIOS.length).toBeGreaterThan(0);
    expect(SUITES.length).toBeGreaterThan(0);
  });

  it("carries the problem statement's criteria, all marked critical", () => {
    const criteria = ALL_SCENARIOS.filter((s) => s.critical);

    expect(criteria.length).toBeGreaterThanOrEqual(5);
    for (const scenario of criteria) {
      // A critical scenario asserting nothing would pass unconditionally.
      const e = scenario.expect;
      const asserts =
        e.decision !== null ||
        e.allowed !== null ||
        e.rules.length > 0 ||
        e.rules_absent.length > 0 ||
        e.message_contains !== null ||
        e.unknown_paths.length > 0;
      expect(asserts, `${scenario.id} asserts nothing`).toBe(true);
    }
  });

  it("asserts rule ids, not only outcomes", () => {
    // Asserting only the decision keeps passing after a rule is deleted, so long as
    // something else happens to stop the same action — precisely the regression the
    // suite exists to catch.
    const withRules = ALL_SCENARIOS.filter(
      (s) => s.expect.rules.length > 0 || s.expect.rules_absent.length > 0,
    );

    expect(withRules.length).toBeGreaterThan(ALL_SCENARIOS.length / 2);
  });

  it("gives every scenario a unique id", () => {
    const ids = ALL_SCENARIOS.map((s) => s.id);
    expect(new Set(ids).size).toBe(ids.length);
  });
});

// ---------------------------------------------------------------------------
// checkExpectation — one case per assertion kind
// ---------------------------------------------------------------------------

describe("checkExpectation", () => {
  it("passes when everything the scenario asserts holds", () => {
    const failures = checkExpectation(
      {
        ...NOTHING_EXPECTED,
        decision: "block",
        allowed: false,
        rules: ["db-bulk-delete"],
        message_contains: "500",
      },
      observed(),
    );

    expect(failures).toEqual([]);
  });

  it("reports a wrong decision, and names what did match", () => {
    const failures = checkExpectation(
      { ...NOTHING_EXPECTED, decision: "allow" },
      observed(),
    );

    expect(failures).toHaveLength(1);
    expect(failures[0]).toContain("expected decision 'allow'");
    expect(failures[0]).toContain("db-bulk-delete");
  });

  it("says so explicitly when no rules matched at all", () => {
    const failures = checkExpectation(
      { ...NOTHING_EXPECTED, decision: "block" },
      observed({ decision: "allow", ruleIds: [] }),
    );

    expect(failures[0]).toContain("no rules matched");
  });

  it("checks allowed independently of the decision", () => {
    // They are separate fields on the wire on purpose: a client that gets log_and_allow
    // wrong in either direction is a security or availability incident.
    const failures = checkExpectation(
      { ...NOTHING_EXPECTED, allowed: true },
      observed({ allowed: false }),
    );

    expect(failures).toEqual(["expected allowed=true, got allowed=false"]);
  });

  it("reports a rule that was required and did not fire", () => {
    const failures = checkExpectation(
      { ...NOTHING_EXPECTED, rules: ["external-email-review"] },
      observed(),
    );

    expect(failures[0]).toContain("external-email-review");
    expect(failures[0]).toContain("db-bulk-delete");
  });

  it("reports a rule that fired and should not have", () => {
    // rules_absent is the assertion that catches a rule becoming too broad — the failure
    // that looks like extra safety and is actually an untested blast radius.
    const failures = checkExpectation(
      { ...NOTHING_EXPECTED, rules_absent: ["db-bulk-delete"] },
      observed(),
    );

    expect(failures).toEqual(["rule(s) db-bulk-delete matched but should not have"]);
  });

  it("checks the message as a substring, not equality", () => {
    expect(
      checkExpectation({ ...NOTHING_EXPECTED, message_contains: "500" }, observed()),
    ).toEqual([]);

    expect(
      checkExpectation({ ...NOTHING_EXPECTED, message_contains: "999" }, observed()),
    ).toHaveLength(1);
  });

  it("treats a null message as empty rather than throwing", () => {
    const failures = checkExpectation(
      { ...NOTHING_EXPECTED, message_contains: "anything" },
      observed({ message: null }),
    );

    expect(failures).toHaveLength(1);
    expect(failures[0]).toContain("anything");
  });

  it("checks that a path was reported UNKNOWN", () => {
    // The fail-closed assertion. A scenario expecting UNKNOWN is checking that the engine
    // could NOT resolve a fact and restrained anyway.
    expect(
      checkExpectation(
        { ...NOTHING_EXPECTED, unknown_paths: ["derived.record_count"] },
        observed({ unknownPaths: ["derived.record_count"] }),
      ),
    ).toEqual([]);

    const failures = checkExpectation(
      { ...NOTHING_EXPECTED, unknown_paths: ["derived.record_count"] },
      observed({ unknownPaths: [] }),
    );
    expect(failures[0]).toContain("reported none");
  });

  it("returns every failure, not the first", () => {
    // Fixing a policy one assertion per run is a slow loop, and the full list usually
    // shows the single underlying cause.
    const failures = checkExpectation(
      {
        ...NOTHING_EXPECTED,
        decision: "allow",
        allowed: true,
        rules: ["some-other-rule"],
        message_contains: "nope",
      },
      observed(),
    );

    expect(failures.length).toBe(4);
  });

  it("asserts nothing when the expectation asserts nothing", () => {
    // The canonical DSL rejects an empty expectation at load time; this documents that
    // the checker itself does not invent an assertion.
    expect(checkExpectation(NOTHING_EXPECTED, observed())).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Reducing a response, and summarising a run
// ---------------------------------------------------------------------------

describe("observe", () => {
  it("tolerates a response missing the optional fields", () => {
    const response = {
      decision: "allow",
      allowed: true,
      matched_rules: [],
      message: null,
      bundle_id: "default",
      bundle_version: 1,
      bundle_source: "active",
      latency_ms: 1,
    } as unknown as SimulateResponse;

    const result = observe(response);

    expect(result.unknownPaths).toEqual([]);
    expect(result.derived).toEqual({});
  });
});

describe("summarise", () => {
  const result = (
    id: string,
    critical: boolean,
    failures: string[],
    error: string | null = null,
  ): ScenarioResult => ({
    scenario: {
      id,
      description: "",
      critical,
      tags: [],
      suite: "s",
      action: { tool: "t", arguments: {}, context: {} },
      expect: NOTHING_EXPECTED,
    },
    observed: error ? null : observed(),
    failures,
    error,
    durationMs: 1,
  });

  it("separates a request error from an assertion failure", () => {
    // An unreachable service is not a policy failure. Reporting it as one sends somebody
    // to edit a rule that is working correctly.
    const summary = summarise(
      [result("a", false, []), result("b", false, [], "network down")],
      10,
    );

    expect(summary.passed).toBe(1);
    expect(summary.failed).toBe(0);
    expect(summary.errored).toBe(1);
  });

  it("counts critical failures separately", () => {
    // So "18 of 20 passed" cannot hide that one failure was a stated requirement.
    const summary = summarise(
      [
        result("ok", true, []),
        result("bad", true, ["boom"]),
        result("meh", false, ["boom"]),
      ],
      10,
    );

    expect(summary.failed).toBe(2);
    expect(summary.criticalFailures).toBe(1);
  });

  it("counts a critical *error* as a critical failure too", () => {
    // A required scenario that could not run has not been satisfied, whatever the reason.
    const summary = summarise([result("bad", true, [], "timeout")], 10);

    expect(summary.criticalFailures).toBe(1);
  });

  it("verdict distinguishes all three states", () => {
    expect(verdict(result("a", false, []))).toBe("pass");
    expect(verdict(result("b", false, ["x"]))).toBe("fail");
    expect(verdict(result("c", false, [], "boom"))).toBe("error");
  });
});

// ---------------------------------------------------------------------------
// Change-impact direction
// ---------------------------------------------------------------------------

describe("compareDecisions", () => {
  const ORDER: Effect[] = ["allow", "log_and_allow", "require_hitl", "block"];

  it("calls a change looser when the candidate restrains less", () => {
    // The one a reviewer must never miss.
    expect(compareDecisions("block", "allow")).toBe("looser");
    expect(compareDecisions("require_hitl", "log_and_allow")).toBe("looser");
    expect(compareDecisions("log_and_allow", "allow")).toBe("looser");
  });

  it("calls a change stricter when the candidate restrains more", () => {
    expect(compareDecisions("allow", "block")).toBe("stricter");
    expect(compareDecisions("log_and_allow", "require_hitl")).toBe("stricter");
  });

  it("reports no change when the decision is identical", () => {
    for (const effect of ORDER) {
      expect(compareDecisions(effect, effect)).toBe("same");
    }
  });

  it("uses the engine's ordering, not alphabetical or arbitrary", () => {
    // block > require_hitl > log_and_allow > allow. Getting this backwards would label a
    // dangerous change as "stricter" and a tightening as a risk — the exact inversion
    // that makes a review tool worse than none.
    for (let i = 0; i < ORDER.length; i += 1) {
      for (let j = 0; j < ORDER.length; j += 1) {
        const expected = i === j ? "same" : j < i ? "looser" : "stricter";
        expect(
          compareDecisions(ORDER[i], ORDER[j]),
          `${ORDER[i]} -> ${ORDER[j]}`,
        ).toBe(expected);
      }
    }
  });
});

// ---------------------------------------------------------------------------
// Bundle parsing
// ---------------------------------------------------------------------------

describe("parseBundle", () => {
  it("round-trips a bundle through YAML", () => {
    const document = { apiVersion: "guardrail/v1", rules: [{ id: "r", effect: "block" }] };

    const parsed = parseBundle(toYaml(document));

    expect(parsed.error).toBeNull();
    expect(parsed.document).toEqual(document);
  });

  it("reports a syntax error with a line number rather than throwing", () => {
    const parsed = parseBundle("rules:\n  - id: a\n   effect: block\n");

    expect(parsed.document).toBeNull();
    expect(parsed.error).toContain("Not valid YAML");
  });

  it("refuses a document that is not a mapping", () => {
    // A bundle must be a mapping; a list would fail server-side with a less useful
    // message after a round trip.
    expect(parseBundle("- a\n- b").error).toContain("must be a mapping");
    expect(parseBundle("just a string").error).toContain("must be a mapping");
  });

  it("refuses an empty draft rather than publishing nothing", () => {
    expect(parseBundle("   ").error).toBe("The bundle is empty.");
  });

  it("does not execute custom tags", () => {
    // Full YAML can construct arbitrary objects, which is the same class of hole as
    // eval. The server refuses these too, via safe_load.
    const parsed = parseBundle("a: !!js/function 'function(){}'");

    expect(parsed.document).toBeNull();
    expect(parsed.error).toContain("Not valid YAML");
  });
});

describe("rulesOf", () => {
  it("returns nothing for a malformed bundle instead of throwing", () => {
    expect(rulesOf(null)).toEqual([]);
    expect(rulesOf({ rules: "not a list" })).toEqual([]);
    expect(rulesOf({})).toEqual([]);
  });

  it("skips entries that are not rules", () => {
    const rules = rulesOf({ rules: [{ id: "good", effect: "block" }, "nonsense", null] });

    expect(rules).toHaveLength(1);
    expect(rules[0].id).toBe("good");
  });
});
