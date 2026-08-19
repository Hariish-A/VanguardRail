/**
 * Change Impact — what a candidate policy would decide differently.
 *
 * ## The question this answers
 *
 * "I changed a threshold from 100 to 250. What does that actually permit?" A diff of the
 * YAML answers a different, easier question — which characters moved. This runs a corpus
 * of real actions through **both** policies and reports where the decisions diverge.
 *
 * ## Why LOOSER is called out rather than just "changed"
 *
 * A summary reading "3 decisions changed" is a fact. "One action that is blocked today
 * would be allowed" is a decision to make. Those are not the same message, and only the
 * second one stops a reviewer scrolling past.
 *
 * Direction is computed from the effect ordering the engine itself uses —
 * `block > require_hitl > log_and_allow > allow` — so "looser" means precisely "the
 * candidate restrains less", not "the text differs".
 *
 * ## Nothing here is recorded
 *
 * Both sides run through `/v1/simulate`, which writes no audit record and creates no
 * pending decision. Reviewing a policy change should not require publishing it, and
 * exploring one should not fill the tamper-evident log with speculation. The cost, stated
 * on the page: a simulation leaves no trace, so this exploration is not itself in the
 * chain.
 */

import { motion } from "framer-motion";
import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import { ALL_SCENARIOS } from "@/lib/conformance";
import { cn } from "@/lib/format";
import {
  compareDecisions,
  DIRECTION_STYLE,
  parseBundle,
  toYaml,
  type Direction,
} from "@/lib/policy";
import { useAsync, useSession } from "@/lib/store";
import type { SimulateResponse } from "@/lib/types";
import { DecisionBadge } from "@/components/DecisionBadge";
import { GlowCard } from "@/components/effects";
import {
  Button,
  Card,
  EmptyState,
  ErrorNote,
  SectionTitle,
  Select,
  Skeleton,
  Textarea,
} from "@/components/ui";

interface Row {
  id: string;
  description: string;
  tool: string;
  args: Record<string, unknown>;
  active: SimulateResponse | null;
  candidate: SimulateResponse | null;
  direction: Direction | null;
  error: string | null;
}

type Source = "draft" | "version";

export function ChangeImpactPage() {
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

  const [source, setSource] = useState<Source>("draft");
  const [candidateVersion, setCandidateVersion] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const [rows, setRows] = useState<Row[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [progress, setProgress] = useState(0);
  const [onlyChanged, setOnlyChanged] = useState(true);

  const parsed = parseBundle(draft);

  const run = async () => {
    setError(null);
    setRows([]);
    setProgress(0);

    let against: { version?: number; bundle?: Record<string, unknown> };
    if (source === "version") {
      if (candidateVersion === null) {
        setError(new Error("Choose a published version to compare against."));
        return;
      }
      against = { version: candidateVersion };
    } else {
      if (!parsed.document) {
        setError(new Error(parsed.error ?? "The draft is not a usable bundle."));
        return;
      }
      against = { bundle: parsed.document };
    }

    setRunning(true);
    const collected: Row[] = [];

    // Sequential, two requests per scenario. The tenant is rate limited and the table is
    // provisioned at 5 write units; forty concurrent requests from a browser would return
    // 429s that read as policy differences.
    for (const [index, scenario] of ALL_SCENARIOS.entries()) {
      const envelope = {
        agent_id: "console-impact",
        session_id: `impact-${index}`,
        tool: scenario.action.tool,
        arguments: scenario.action.arguments,
      };

      let row: Row;
      try {
        const [activeResult, candidateResult] = [
          await api.simulateAgainst(session, envelope),
          await api.simulateAgainst(session, envelope, against),
        ];
        row = {
          id: scenario.id,
          description: scenario.description,
          tool: scenario.action.tool,
          args: scenario.action.arguments,
          active: activeResult,
          candidate: candidateResult,
          direction: compareDecisions(activeResult.decision, candidateResult.decision),
          error: null,
        };
      } catch (cause) {
        row = {
          id: scenario.id,
          description: scenario.description,
          tool: scenario.action.tool,
          args: scenario.action.arguments,
          active: null,
          candidate: null,
          direction: null,
          error: cause instanceof Error ? cause.message : String(cause),
        };
        // A rejected candidate bundle is a fact about the bundle, not about one scenario.
        // Stopping is honest; forty identical 422s would look like forty findings.
        if (cause instanceof ApiError && cause.status === 422) {
          collected.push(row);
          setRows([...collected]);
          setError(cause);
          break;
        }
      }

      collected.push(row);
      setRows([...collected]);
      setProgress(index + 1);
    }

    setRunning(false);
  };

  const changed = rows.filter((r) => r.direction && r.direction !== "same");
  const looser = rows.filter((r) => r.direction === "looser");
  const stricter = rows.filter((r) => r.direction === "stricter");
  const errored = rows.filter((r) => r.error !== null);
  const visible = onlyChanged ? changed : rows;

  if (!connected) {
    return (
      <EmptyState
        icon="⇄"
        title="Not connected"
        detail="Change-impact analysis simulates against your tenant's active policy, which comes from the API key."
        action={<Button onClick={() => (window.location.hash = "/connect")}>Connect</Button>}
      />
    );
  }

  return (
    <div className="space-y-7">
      <div>
        <h1 className="text-[26px] font-semibold tracking-tight text-ink-100">
          Change Impact
        </h1>
        <p className="mt-2 max-w-3xl text-[14px] leading-relaxed text-ink-400">
          Runs the full scenario corpus through the active policy and a candidate, and
          reports where they disagree. A YAML diff tells you which characters moved; this
          tells you which <em>actions</em> would be decided differently — and, specifically,
          which would become permitted.
        </p>
      </div>

      <Card className="p-5">
        <SectionTitle
          title="Candidate"
          hint="An unpublished draft, or an already-published version. Neither is activated by running this."
        />

        <div className="mb-4 flex flex-wrap gap-2">
          {(
            [
              ["draft", "Unpublished draft"],
              ["version", "A published version"],
            ] as Array<[Source, string]>
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              onClick={() => setSource(value)}
              className={cn(
                "rounded-lg border px-3 py-1.5 text-[12.5px] transition-colors",
                source === value
                  ? "border-brand-600 bg-brand-500/10 text-brand-400"
                  : "border-ink-700 text-ink-400 hover:border-ink-600 hover:text-ink-200",
              )}
            >
              {label}
            </button>
          ))}
        </div>

        {source === "version" ? (
          <div className="max-w-sm">
            {versions.loading ? (
              <Skeleton className="h-11 w-full" />
            ) : (
              <Select
                value={candidateVersion === null ? "" : String(candidateVersion)}
                onChange={(event) =>
                  setCandidateVersion(
                    event.target.value === "" ? null : Number(event.target.value),
                  )
                }
              >
                <option value="">Choose a version…</option>
                {[...(versions.data?.versions ?? [])]
                  .sort((a, b) => b.version - a.version)
                  .map((v) => (
                    <option key={v.version} value={v.version}>
                      v{v.version}
                      {v.is_active ? " (in force)" : ""} — {v.description || "no description"}
                    </option>
                  ))}
              </Select>
            )}
            {(versions.data?.versions.length ?? 0) === 0 && !versions.loading && (
              <p className="mt-2 text-[12.5px] text-ink-500">
                Nothing published yet. Author a draft instead, or publish one in{" "}
                <a className="text-brand-400 underline" href="#/policy">
                  Policy Studio
                </a>
                .
              </p>
            )}
          </div>
        ) : (
          <>
            <div className="mb-2 flex flex-wrap gap-2">
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
              rows={12}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="Paste or edit a candidate bundle…"
              aria-label="Candidate bundle YAML"
            />
            {parsed.error && draft.trim() !== "" && (
              <p className="mt-2 text-[12.5px] text-block">{parsed.error}</p>
            )}
          </>
        )}

        <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-ink-800 pt-4">
          <Button
            loading={running}
            disabled={running}
            onClick={() => void run()}
          >
            Compare against what is in force
          </Button>
          <span className="text-[12px] text-ink-500">
            {ALL_SCENARIOS.length} actions × 2 simulations. Writes nothing, activates
            nothing.
          </span>
          {running && (
            <span className="font-mono text-[12px] text-brand-400">
              {progress}/{ALL_SCENARIOS.length}
            </span>
          )}
        </div>
      </Card>

      <ErrorNote error={error} />

      {rows.length > 0 && (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <GlowCard
              className="p-5"
              accent={looser.length > 0 ? "var(--color-block)" : "var(--color-ink-500)"}
            >
              <div
                className={cn(
                  "font-mono text-[26px] font-semibold",
                  looser.length > 0 ? "text-block" : "text-ink-300",
                )}
              >
                {looser.length}
              </div>
              <div className="mt-1 text-[13px] text-ink-200">would become permitted</div>
              <div className="mt-0.5 text-[12px] leading-snug text-ink-500">
                The candidate restrains less than the active policy. Read every one.
              </div>
            </GlowCard>

            <Card className="p-5">
              <div className="font-mono text-[26px] font-semibold text-hitl">
                {stricter.length}
              </div>
              <div className="mt-1 text-[13px] text-ink-200">would become restrained</div>
              <div className="mt-0.5 text-[12px] leading-snug text-ink-500">
                Usually the intent. Confirm it is.
              </div>
            </Card>

            <Card className="p-5">
              <div className="font-mono text-[26px] font-semibold text-ink-100">
                {rows.length - changed.length - errored.length}
              </div>
              <div className="mt-1 text-[13px] text-ink-200">unchanged</div>
            </Card>

            <Card className="p-5">
              <div
                className={cn(
                  "font-mono text-[26px] font-semibold",
                  errored.length > 0 ? "text-block" : "text-ink-100",
                )}
              >
                {errored.length}
              </div>
              <div className="mt-1 text-[13px] text-ink-200">could not be compared</div>
              <div className="mt-0.5 text-[12px] leading-snug text-ink-500">
                A request failure, not a policy difference.
              </div>
            </Card>
          </div>

          {changed.length === 0 && errored.length === 0 && (
            <Card className="border-[color-mix(in_oklab,var(--color-allow)_35%,transparent)] p-5">
              <p className="text-[13.5px] text-ink-200">
                No decision in the corpus changes.
              </p>
              <p className="mt-2 text-[12.5px] leading-relaxed text-ink-500">
                That is a real result, and also a limit worth stating: it means nothing in{" "}
                <em>this corpus</em> changes. The corpus covers the problem statement's
                criteria and the engine's properties, not every action your agents make. A
                policy change that only affects a tool no scenario exercises will show as
                no impact here.
              </p>
            </Card>
          )}

          <div>
            <SectionTitle
              title="Per action"
              action={
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setOnlyChanged((value) => !value)}
                >
                  {onlyChanged ? `Show all ${rows.length}` : "Show only differences"}
                </Button>
              }
            />
            <div className="space-y-2">
              {visible.map((row) => {
                const style = row.direction
                  ? DIRECTION_STYLE[row.direction]
                  : DIRECTION_STYLE.same;
                return (
                  <motion.div
                    key={row.id}
                    initial={{ opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: 0.2 }}
                  >
                    <Card className={cn("p-4", row.direction !== "same" && style.border)}>
                      <div className="flex flex-wrap items-center gap-3">
                        <span className="font-mono text-[12.5px] text-ink-100">
                          {row.tool}
                        </span>
                        <span className="font-mono text-[11px] text-ink-500">{row.id}</span>
                        {row.direction && row.direction !== "same" && (
                          <span
                            className={cn(
                              "font-mono text-[11px] font-semibold tracking-wide",
                              style.text,
                            )}
                          >
                            {style.label}
                          </span>
                        )}
                      </div>

                      {row.error ? (
                        <p className="mt-2 text-[12.5px] text-block">{row.error}</p>
                      ) : (
                        <div className="mt-3 flex flex-wrap items-center gap-3">
                          <span className="text-[11px] uppercase tracking-wider text-ink-500">
                            in force
                          </span>
                          <DecisionBadge
                            effect={row.active?.decision ?? "allow"}
                            size="sm"
                          />
                          <span className="text-ink-600">→</span>
                          <span className="text-[11px] uppercase tracking-wider text-ink-500">
                            candidate
                          </span>
                          <DecisionBadge
                            effect={row.candidate?.decision ?? "allow"}
                            size="sm"
                          />
                          <span className="ml-auto font-mono text-[11px] text-ink-600">
                            {row.active?.matched_rules.map((r) => r.rule_id).join(", ") ||
                              "no rules"}{" "}
                            →{" "}
                            {row.candidate?.matched_rules.map((r) => r.rule_id).join(", ") ||
                              "no rules"}
                          </span>
                        </div>
                      )}

                      {row.description && (
                        <p className="mt-2 text-[12px] leading-relaxed text-ink-500">
                          {row.description}
                        </p>
                      )}
                    </Card>
                  </motion.div>
                );
              })}
            </div>
          </div>
        </>
      )}

      <Card className="p-5">
        <SectionTitle title="What this does not tell you" />
        <ul className="space-y-2 text-[13px] leading-relaxed text-ink-300">
          <li>
            · <strong className="text-ink-100">Coverage is the corpus.</strong> Twenty
            actions covering the problem statement's criteria and the engine's properties.
            A change affecting a tool no scenario exercises reports as no impact.
          </li>
          <li>
            · <strong className="text-ink-100">Nothing is recorded.</strong> Both sides run
            through <span className="font-mono">/v1/simulate</span>, so this exploration
            leaves no trace in the audit chain — deliberate, so speculation does not dilute
            evidence, but it means the chain will not show that you checked.
          </li>
          <li>
            · <strong className="text-ink-100">A candidate is not activated.</strong>{" "}
            Comparing is free. Making it real is{" "}
            <a className="text-brand-400 underline" href="#/policy">
              publish, then activate
            </a>
            , and those are two separate acts on purpose.
          </li>
        </ul>
      </Card>
    </div>
  );
}
