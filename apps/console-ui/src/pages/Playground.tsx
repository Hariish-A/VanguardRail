/**
 * Playground — which rules actually do anything, and why.
 *
 * ## The question worth asking of a policy
 *
 * Not "does it block the bad thing" — the conformance suite answers that. The question
 * here is **which rules ever fire**. A rule that matches nothing looks exactly like
 * coverage: it sits in the file, it reads convincingly, it is counted in "12 rules", and
 * it defends nothing.
 *
 * That failure mode has happened three times in this repository's own tests, and the
 * lesson each time was the same: *a check that cannot fire is worse than no check,
 * because it manufactures confidence*. Policy has the identical failure mode, so the
 * matrix below runs every corpus action against every rule and names the rules that never
 * matched.
 *
 * A rule can be silent for two very different reasons, and the page says so rather than
 * accusing: the corpus may not exercise it, or its predicate may be unsatisfiable. Only
 * the author can tell which — but they cannot even ask until somebody shows them.
 *
 * ## Against any version, recording nothing
 *
 * Everything here is `/v1/simulate`, which writes no audit record and creates no pending
 * decision, and which accepts a published version or an inline draft. So a rule in v3 can
 * be probed long after v7 took over.
 */

import { useMemo, useState } from "react";
import { api } from "@/lib/api";
import { ALL_SCENARIOS } from "@/lib/conformance";
import { prettyJson } from "@/lib/format";
import { parseBundle, rulesOf, toYaml } from "@/lib/policy";
import { useAsync, useSession } from "@/lib/store";
import type { PolicyRule, SimulateResponse } from "@/lib/types";
import { DecisionBadge } from "@/components/DecisionBadge";
import { GlowCard } from "@/components/effects";
import {
  Badge,
  Button,
  Card,
  CodeBlock,
  EmptyState,
  ErrorNote,
  Field,
  KeyValue,
  SectionTitle,
  Select,
  Skeleton,
  Textarea,
} from "@/components/ui";

type Against = { version?: number; bundle?: Record<string, unknown> };

export function PlaygroundPage() {
  const { session, status } = useSession();
  const connected = status === "connected";

  const versions = useAsync(
    () => (connected ? api.policies(session) : Promise.resolve(null)),
    [connected, session.baseUrl, session.apiKey],
  );
  const active = useAsync(
    () => (connected ? api.activePolicy(session) : Promise.resolve(null)),
    [connected, session.baseUrl, session.apiKey],
  );

  const [selected, setSelected] = useState<string>("active");
  const [draft, setDraft] = useState("");
  const [matrix, setMatrix] = useState<Record<string, string[]> | null>(null);
  const [building, setBuilding] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const [probeTool, setProbeTool] = useState("db.delete_records");
  const [probeArgs, setProbeArgs] = useState(
    prettyJson({ table: "users", count: 500, where: "last_login < '2024-01-01'" }),
  );
  const [probe, setProbe] = useState<SimulateResponse | null>(null);
  const [probing, setProbing] = useState(false);

  const draftParsed = parseBundle(draft);

  const against = (): Against => {
    if (selected === "active") return {};
    if (selected === "draft") {
      if (!draftParsed.document) throw new Error(draftParsed.error ?? "unusable draft");
      return { bundle: draftParsed.document };
    }
    return { version: Number(selected) };
  };

  /** Rules known locally, for the "never fired" comparison. */
  const knownRules: PolicyRule[] = useMemo(() => {
    if (selected === "draft") return rulesOf(draftParsed.document);
    if (selected === "active" && active.data) {
      return rulesOf(active.data.document as Record<string, unknown>);
    }
    return [];
  }, [selected, draftParsed.document, active.data]);

  const buildMatrix = async () => {
    setBuilding(true);
    setError(null);
    setMatrix(null);
    try {
      const target = against();
      const result: Record<string, string[]> = {};
      for (const [index, scenario] of ALL_SCENARIOS.entries()) {
        const response = await api.simulateAgainst(
          session,
          {
            agent_id: "console-playground",
            session_id: `playground-${index}`,
            tool: scenario.action.tool,
            arguments: scenario.action.arguments,
          },
          target,
        );
        result[scenario.id] = response.matched_rules.map((rule) => rule.rule_id);
      }
      setMatrix(result);
    } catch (cause) {
      setError(cause);
    } finally {
      setBuilding(false);
    }
  };

  const runProbe = async () => {
    setProbing(true);
    setError(null);
    setProbe(null);
    try {
      setProbe(
        await api.simulateAgainst(
          session,
          {
            agent_id: "console-playground",
            session_id: `probe-${Date.now().toString(36)}`,
            tool: probeTool,
            arguments: JSON.parse(probeArgs) as Record<string, unknown>,
          },
          against(),
        ),
      );
    } catch (cause) {
      setError(
        cause instanceof SyntaxError
          ? new Error(`Arguments are not valid JSON: ${cause.message}`)
          : cause,
      );
    } finally {
      setProbing(false);
    }
  };

  const firedRules = useMemo(() => {
    if (!matrix) return new Set<string>();
    return new Set(Object.values(matrix).flat());
  }, [matrix]);

  const silentRules = knownRules.filter((rule) => !firedRules.has(rule.id));

  if (!connected) {
    return (
      <EmptyState
        icon="◎"
        title="Not connected"
        detail="Simulation resolves the caller's own policy — it would otherwise be an inviting way to read another tenant's rules back one decision at a time."
        action={<Button onClick={() => (window.location.hash = "/connect")}>Connect</Button>}
      />
    );
  }

  return (
    <div className="space-y-7">
      <div>
        <h1 className="text-[26px] font-semibold tracking-tight text-ink-100">Playground</h1>
        <p className="mt-1.5 text-[13.5px] text-ink-400">
          Probe any policy version or an unpublished draft. Records nothing.
        </p>
      </div>

      <Card className="p-5">
        <SectionTitle title="Policy to explore" />
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Source">
            {versions.loading ? (
              <Skeleton className="h-11 w-full" />
            ) : (
              <Select value={selected} onChange={(event) => setSelected(event.target.value)}>
                <option value="active">
                  Active{active.data ? ` (v${active.data.version})` : ""}
                </option>
                {[...(versions.data?.versions ?? [])]
                  .sort((a, b) => b.version - a.version)
                  .map((v) => (
                    <option key={v.version} value={String(v.version)}>
                      v{v.version}
                      {v.is_active ? " (in force)" : ""}
                    </option>
                  ))}
                <option value="draft">An unpublished draft…</option>
              </Select>
            )}
          </Field>
          <div className="flex items-end">
            <Button
              variant="outline"
              loading={building}
              disabled={building}
              onClick={() => void buildMatrix()}
            >
              Build the rule matrix
            </Button>
          </div>
        </div>

        {selected === "draft" && (
          <div className="mt-4">
            <div className="mb-2 flex gap-2">
              {active.data && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setDraft(toYaml(active.data!.document))}
                >
                  Start from the active bundle
                </Button>
              )}
            </div>
            <Textarea
              rows={10}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="Paste a candidate bundle…"
              aria-label="Draft bundle YAML"
            />
            {draftParsed.error && draft.trim() !== "" && (
              <p className="mt-2 text-[12.5px] text-block">{draftParsed.error}</p>
            )}
          </div>
        )}
      </Card>

      <ErrorNote error={error} />

      {/* ---------------------------------------------------------- dead rules */}
      {matrix && knownRules.length > 0 && (
        <GlowCard
          className="p-5"
          accent={silentRules.length > 0 ? "var(--color-hitl)" : "var(--color-allow)"}
        >
          <SectionTitle
            title={
              silentRules.length === 0
                ? "Every rule fired at least once"
                : `${silentRules.length} rule${silentRules.length === 1 ? "" : "s"} never fired`
            }
            hint="Across the whole corpus."
          />
          {silentRules.length === 0 ? (
            <p className="text-[13px] leading-relaxed text-ink-300">
              Each of the {knownRules.length} rules matched at least one action.
            </p>
          ) : (
            <>
              <div className="flex flex-wrap gap-2">
                {silentRules.map((rule) => (
                  <Badge key={rule.id} tone="warn">
                    {rule.id}
                  </Badge>
                ))}
              </div>
              <p className="mt-3 text-[12.5px] leading-relaxed text-ink-400">
                Either the corpus does not exercise it, or its predicate cannot be
                satisfied. This page cannot tell the two apart.
              </p>
            </>
          )}
        </GlowCard>
      )}

      {/* ------------------------------------------------------------- matrix */}
      {matrix && (
        <Card className="p-5">
          <SectionTitle
            title="Rule matrix"
            hint="Which rules matched each action."
          />
          <div className="overflow-x-auto">
            <table className="w-full min-w-[640px] text-left">
              <thead>
                <tr className="border-b border-ink-800">
                  <th className="pb-2 pr-4 text-[11px] uppercase tracking-wider text-ink-500">
                    Action
                  </th>
                  <th className="pb-2 pr-4 text-[11px] uppercase tracking-wider text-ink-500">
                    Tool
                  </th>
                  <th className="pb-2 text-[11px] uppercase tracking-wider text-ink-500">
                    Rules matched
                  </th>
                </tr>
              </thead>
              <tbody>
                {ALL_SCENARIOS.map((scenario) => {
                  const fired = matrix[scenario.id] ?? [];
                  return (
                    <tr
                      key={scenario.id}
                      className="border-b border-ink-800/60 last:border-0"
                    >
                      <td className="py-2.5 pr-4 font-mono text-[12px] text-ink-300">
                        {scenario.id}
                      </td>
                      <td className="py-2.5 pr-4 font-mono text-[12px] text-ink-400">
                        {scenario.action.tool}
                      </td>
                      <td className="py-2.5">
                        {fired.length === 0 ? (
                          <span className="text-[12px] text-ink-600">
                            none — the bundle default applied
                          </span>
                        ) : (
                          <div className="flex flex-wrap gap-1.5">
                            {fired.map((rule) => (
                              <Badge key={rule} tone="brand">
                                {rule}
                              </Badge>
                            ))}
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {/* -------------------------------------------------------------- probe */}
      <div className="grid gap-5 lg:grid-cols-2">
        <Card className="p-5">
          <SectionTitle
            title="Probe one action"
            hint="Against the policy selected above. Records nothing."
          />
          <div className="space-y-4">
            <Field label="Tool">
              <Select
                value={probeTool}
                onChange={(event) => setProbeTool(event.target.value)}
              >
                {[
                  "db.delete_records",
                  "email.send",
                  "file.read",
                  "http.request",
                  "payments.refund",
                  "mcp.filesystem.read_file",
                  "mcp.filesystem.write_file",
                ].map((tool) => (
                  <option key={tool} value={tool}>
                    {tool}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Arguments (JSON)">
              <Textarea
                rows={8}
                value={probeArgs}
                onChange={(event) => setProbeArgs(event.target.value)}
              />
            </Field>
            <Button loading={probing} disabled={probing} onClick={() => void runProbe()}>
              Simulate
            </Button>
          </div>
        </Card>

        <Card className="p-5">
          <SectionTitle
            title="Derived facts"
            hint="What the rules were matched against."
          />
          {!probe ? (
            <p className="text-[13px] leading-relaxed text-ink-500">
              Run a probe. Rules never see raw arguments — they see normalised facts, which
              is why <span className="font-mono">record_count</span> is the same fact
              whether the agent passed a count, a list of ids, or a WHERE clause.
            </p>
          ) : (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center gap-3">
                <DecisionBadge effect={probe.decision} size="md" animate />
                <Badge>
                  {probe.bundle_source} v{probe.bundle_version}
                </Badge>
                <Badge>{probe.latency_ms.toFixed(1)} ms</Badge>
              </div>

              {Object.keys(probe.derived).length === 0 ? (
                <p className="text-[12.5px] text-ink-500">
                  No facts were derived for this tool.
                </p>
              ) : (
                <div className="space-y-1.5">
                  {Object.entries(probe.derived).map(([key, value]) => (
                    <div
                      key={key}
                      className="flex items-baseline justify-between gap-4 rounded-lg border border-ink-800 bg-ink-900/40 px-3 py-2"
                    >
                      <span className="font-mono text-[12px] text-ink-400">
                        derived.{key}
                      </span>
                      <span className="truncate font-mono text-[12px] text-ink-100">
                        {typeof value === "object" ? JSON.stringify(value) : String(value)}
                      </span>
                    </div>
                  ))}
                </div>
              )}

              {probe.unknown_paths.length > 0 && (
                <div className="rounded-lg border border-[color-mix(in_oklab,var(--color-hitl)_35%,transparent)] p-3">
                  <div className="text-[11px] uppercase tracking-wider text-hitl">
                    UNKNOWN — failed closed
                  </div>
                  <div className="mt-1 font-mono text-[12px] text-ink-300">
                    {probe.unknown_paths.join(", ")}
                  </div>
                </div>
              )}

              <div className="grid gap-3 sm:grid-cols-2">
                <KeyValue
                  label="Rules matched"
                  value={
                    probe.matched_rules.map((r) => r.rule_id).join(", ") || "none"
                  }
                  mono
                />
                <KeyValue label="Allowed" value={String(probe.allowed)} mono />
              </div>

              {probe.message && (
                <p className="rounded-lg border border-ink-800 bg-ink-900/40 p-3 text-[12.5px] leading-relaxed text-ink-300">
                  {probe.message}
                </p>
              )}

              <details className="group">
                <summary className="cursor-pointer list-none text-[12px] text-ink-500 hover:text-ink-300">
                  <span className="inline-block transition-transform group-open:rotate-90">
                    ▸
                  </span>{" "}
                  raw response
                </summary>
                <CodeBlock className="mt-2" maxHeight="16rem" code={prettyJson(probe)} />
              </details>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
