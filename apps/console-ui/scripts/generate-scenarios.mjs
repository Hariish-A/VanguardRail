/**
 * Compile `scenarios/*.yaml` into a JSON module the console can import.
 *
 * ## Why generate rather than commit a copy
 *
 * The conformance page runs the *real* corpus — the same files `guardrail-sim` runs in
 * CI and the same ones that gate a pull request. A hand-maintained copy would drift, and
 * a conformance report built from a stale copy is worse than none: it reports green
 * against scenarios nobody is enforcing any more.
 *
 * This runs on every `dev` and `build`, so the console cannot be built against anything
 * but the current corpus. `test_console_scenarios_match_the_real_corpus` in the Python
 * suite asserts the generated file is in step with the YAML, so a checked-in generated
 * file that someone edited by hand fails the build rather than being served.
 *
 * ## What it deliberately does not do
 *
 * No validation beyond "is this parseable YAML with the fields we read". The canonical
 * validator is `guardrail-sim validate`, which runs in CI against the same files and
 * rejects far more (unknown keys, empty expectations, unparseable regexes). Duplicating
 * that here would create a second, weaker source of truth about what a valid scenario is.
 */

import { readFileSync, readdirSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { load as parseYaml } from "js-yaml";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..", "..", "..");
const scenarioDir = join(repoRoot, "scenarios");
const outFile = join(here, "..", "src", "generated", "scenarios.json");

function loadSuite(file) {
  const raw = readFileSync(join(scenarioDir, file), "utf8");
  const doc = parseYaml(raw);

  if (!doc || typeof doc !== "object") {
    throw new Error(`${file}: expected a mapping at the top level`);
  }

  const defaults = doc.defaults ?? {};
  const scenarios = (doc.scenarios ?? [])
    // `enabled: false` is how a scenario is parked. Shipping it would report a skip the
    // canonical runner does not report.
    .filter((s) => s.enabled !== false)
    .map((s) => ({
      id: s.id,
      description: s.description ?? "",
      critical: Boolean(s.critical),
      tags: s.tags ?? [],
      action: {
        tool: s.action.tool,
        arguments: s.action.arguments ?? {},
        context: { ...(defaults.context ?? {}), ...(s.action.context ?? {}) },
      },
      expect: {
        decision: s.expect.decision ?? null,
        allowed: s.expect.allowed ?? null,
        rules: s.expect.rules ?? [],
        rules_absent: s.expect.rules_absent ?? [],
        message_contains: s.expect.message_contains ?? null,
        unknown_paths: s.expect.unknown_paths ?? [],
      },
    }));

  return {
    file,
    name: doc.name ?? file,
    description: (doc.description ?? "").trim(),
    agent_id: defaults.agent_id ?? "guardrail-sim",
    scenarios,
  };
}

const files = readdirSync(scenarioDir)
  .filter((f) => f.endsWith(".yaml") || f.endsWith(".yml"))
  .sort();

if (files.length === 0) {
  throw new Error(`No scenario files found in ${scenarioDir}. Refusing to emit an empty corpus.`);
}

const suites = files.map(loadSuite);
const total = suites.reduce((n, s) => n + s.scenarios.length, 0);

// An empty corpus would make the conformance page report "0 of 0 passed" in green,
// which is the most misleading thing a test report can say.
if (total === 0) {
  throw new Error("Every scenario file parsed to zero scenarios. Refusing to emit.");
}

const ids = suites.flatMap((s) => s.scenarios.map((x) => x.id));
const duplicates = ids.filter((id, i) => ids.indexOf(id) !== i);
if (duplicates.length > 0) {
  throw new Error(`Duplicate scenario ids across suites: ${[...new Set(duplicates)].join(", ")}`);
}

mkdirSync(dirname(outFile), { recursive: true });
writeFileSync(outFile, `${JSON.stringify({ suites }, null, 2)}\n`, "utf8");

console.log(
  `scenarios: ${total} across ${suites.length} suite(s) -> src/generated/scenarios.json`,
);
