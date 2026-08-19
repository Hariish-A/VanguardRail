/**
 * Client-side helpers for working with policy bundles.
 *
 * ## Why YAML is parsed here at all
 *
 * Policy is authored and reviewed as YAML — that is what lands in a pull request — and
 * the API accepts raw YAML text for `validate` and `publish` precisely so nobody has to
 * convert it by hand. But `/v1/simulate` takes a parsed `bundle`, because it evaluates
 * rather than stores. So a draft that has not been published can only be simulated if the
 * client parses it.
 *
 * That is the whole reason `js-yaml` is a runtime dependency. Change-impact analysis
 * *before* publishing is the point of the feature; requiring a publish first would mean
 * writing to the version history to find out whether you wanted to.
 *
 * **`load`, never `loadAll`, and never the unsafe schema.** The default schema refuses
 * custom tags. Full YAML can construct arbitrary objects, which is the same class of hole
 * as `eval` — the server refuses it for the same reason, using `yaml.safe_load`.
 */

import { load as parseYaml, dump as dumpYaml, YAMLException } from "js-yaml";
import type { Effect, PolicyRule } from "./types";

export interface ParsedBundle {
  document: Record<string, unknown> | null;
  error: string | null;
}

export function parseBundle(text: string): ParsedBundle {
  if (!text.trim()) {
    return { document: null, error: "The bundle is empty." };
  }

  let parsed: unknown;
  try {
    parsed = parseYaml(text);
  } catch (cause) {
    const message =
      cause instanceof YAMLException
        ? `${cause.reason}${cause.mark ? ` (line ${cause.mark.line + 1})` : ""}`
        : String(cause);
    return { document: null, error: `Not valid YAML: ${message}` };
  }

  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return {
      document: null,
      error: `A policy bundle must be a mapping, got ${
        Array.isArray(parsed) ? "a list" : typeof parsed
      }.`,
    };
  }

  return { document: parsed as Record<string, unknown>, error: null };
}

export function toYaml(document: unknown): string {
  return dumpYaml(document, { indent: 2, lineWidth: 96, noRefs: true, sortKeys: false });
}

/** Rules out of a bundle document, tolerating a malformed one rather than throwing. */
export function rulesOf(document: Record<string, unknown> | null): PolicyRule[] {
  if (!document) return [];
  const rules = document.rules;
  if (!Array.isArray(rules)) return [];
  return rules.filter(
    (rule): rule is PolicyRule =>
      typeof rule === "object" && rule !== null && typeof (rule as PolicyRule).id === "string",
  );
}

export function bundleMode(document: Record<string, unknown> | null): string {
  const metadata = document?.metadata;
  if (typeof metadata === "object" && metadata !== null) {
    const mode = (metadata as Record<string, unknown>).mode;
    if (typeof mode === "string") return mode;
  }
  return "enforce";
}

/**
 * How one decision compares to another, in the direction that matters for review.
 *
 * `LOOSER` is the one a reviewer must never miss: a change that permits something the
 * current policy refuses. It is deliberately named from the *security* point of view
 * rather than as "changed", because "three decisions changed" is a fact and "one action
 * that is blocked today would be allowed" is a decision to make.
 */
export type Direction = "same" | "looser" | "stricter";

const SEVERITY: Record<Effect, number> = {
  allow: 0,
  log_and_allow: 1,
  require_hitl: 2,
  block: 3,
};

export function compareDecisions(active: Effect, candidate: Effect): Direction {
  if (active === candidate) return "same";
  return SEVERITY[candidate] < SEVERITY[active] ? "looser" : "stricter";
}

export const DIRECTION_STYLE: Record<
  Direction,
  { label: string; text: string; border: string; note: string }
> = {
  looser: {
    label: "LOOSER",
    text: "text-block",
    border: "border-[color-mix(in_oklab,var(--color-block)_45%,transparent)]",
    note: "The candidate permits something the active policy restrains. Read every one of these.",
  },
  stricter: {
    label: "STRICTER",
    text: "text-hitl",
    border: "border-[color-mix(in_oklab,var(--color-hitl)_45%,transparent)]",
    note: "The candidate restrains something the active policy permits. Usually intended — confirm it is.",
  },
  same: {
    label: "UNCHANGED",
    text: "text-ink-400",
    border: "border-ink-700",
    note: "Both policies reach the same decision.",
  },
};

/** A minimal, valid bundle to start a draft from. */
export const STARTER_BUNDLE = `apiVersion: guardrail/v1
metadata:
  bundle_id: default
  # enforce | shadow. In shadow, block and require_hitl are recorded but not enforced,
  # so a policy can be trialled against live traffic before it restrains anyone.
  mode: enforce
defaults:
  effect: allow
  resolution: most_restrictive
rules:
  - id: db-bulk-delete
    description: Block destructive deletes above the blast-radius threshold
    severity: critical
    match:
      tool: db.delete_records
      all:
        - { path: derived.record_count, op: gt, value: 100 }
    effect: block
    message: "Bulk delete of {derived.record_count} records exceeds the limit of 100."
`;
