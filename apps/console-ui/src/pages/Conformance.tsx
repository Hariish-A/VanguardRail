/**
 * Conformance — the real scenario corpus, run against live AWS, in the browser.
 *
 * ## Which runner is canonical, stated up front
 *
 * `guardrail-sim` is, and it is what gates a pull request. It validates the DSL far more
 * strictly, runs offline against the policy file as well as live, emits JUnit XML for CI,
 * and contains `test_the_suite_actually_fails_when_policy_regresses` — which deletes a
 * rule and requires the suite to go red. That last test is why the suite is worth
 * anything at all.
 *
 * This page runs the *same scenarios*, compiled from `scenarios/*.yaml` at build time so
 * they cannot drift, against the *same deployed endpoint*. It is a live view, not a
 * replacement, and the page says so.
 *
 * ## Critical failures are counted separately
 *
 * Every scenario in `success-criteria.yaml` is marked critical, because a summary line
 * reading "18 of 20 passed" must not be able to hide that one of the two failures was a
 * stated requirement of the problem statement.
 */

import { motion } from "framer-motion";
import { useState } from "react";
import {
  ALL_SCENARIOS,
  runCorpus,
  SUITES,
  verdict,
  type RunSummary,
  type ScenarioResult,
} from "@/lib/conformance";
import { cn, duration, prettyJson } from "@/lib/format";
import { useSession } from "@/lib/store";
import { DecisionBadge } from "@/components/DecisionBadge";
import { GlowCard, ShimmerBorder } from "@/components/effects";
import {
  Badge,
  Button,
  Card,
  CodeBlock,
  EmptyState,
  ErrorNote,
  Tabs,
} from "@/components/ui";

function ResultRow({ result }: { result: ScenarioResult }) {
  const [open, setOpen] = useState(false);
  const state = verdict(result);

  const tone =
    state === "pass"
      ? { dot: "bg-allow", text: "text-allow", label: "PASS" }
      : state === "fail"
        ? { dot: "bg-block", text: "text-block", label: "FAIL" }
        : { dot: "bg-hitl", text: "text-hitl", label: "ERROR" };

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className={cn(
          "w-full rounded-lg border p-3 text-left transition-colors hover:bg-ink-900/70",
          state === "pass"
            ? "border-ink-800 bg-ink-900/40"
            : "border-[color-mix(in_oklab,var(--color-block)_40%,transparent)] bg-[color-mix(in_oklab,var(--color-block)_6%,transparent)]",
        )}
      >
        <div className="flex flex-wrap items-center gap-3">
          <span className={cn("h-2 w-2 shrink-0 rounded-full", tone.dot)} />
          <span className={cn("font-mono text-[11px] font-semibold", tone.text)}>
            {tone.label}
          </span>
          <span className="font-mono text-[12.5px] text-ink-100">{result.scenario.id}</span>
          {result.scenario.critical && <Badge tone="warn">critical</Badge>}
          <span className="font-mono text-[11.5px] text-ink-500">
            {result.scenario.action.tool}
          </span>
          {result.observed && (
            <DecisionBadge effect={result.observed.decision} size="sm" />
          )}
          <span className="ml-auto font-mono text-[11px] text-ink-600">
            {result.durationMs.toFixed(0)} ms
          </span>
        </div>

        {result.scenario.description && (
          <p className="mt-1.5 text-[12px] leading-relaxed text-ink-500">
            {result.scenario.description}
          </p>
        )}

        {result.failures.length > 0 && (
          <ul className="mt-2 space-y-1">
            {result.failures.map((failure, index) => (
              <li key={index} className="font-mono text-[12px] text-block">
                · {failure}
              </li>
            ))}
          </ul>
        )}

        {result.error && (
          <p className="mt-2 font-mono text-[12px] text-hitl">
            request failed: {result.error}
          </p>
        )}
      </button>

      {open && (
        <div className="mt-2 grid gap-3 rounded-lg border border-ink-800 bg-ink-950/50 p-4 lg:grid-cols-2">
          <div>
            <div className="mb-2 text-[11px] uppercase tracking-wider text-ink-500">
              Expected
            </div>
            <CodeBlock maxHeight="14rem" code={prettyJson(result.scenario.expect)} />
          </div>
          <div>
            <div className="mb-2 text-[11px] uppercase tracking-wider text-ink-500">
              Observed
            </div>
            <CodeBlock
              maxHeight="14rem"
              code={prettyJson(result.observed ?? { error: result.error })}
            />
          </div>
          <div className="lg:col-span-2">
            <div className="mb-2 text-[11px] uppercase tracking-wider text-ink-500">
              Action sent
            </div>
            <CodeBlock maxHeight="10rem" code={prettyJson(result.scenario.action)} />
          </div>
        </div>
      )}
    </div>
  );
}

type Filter = "all" | "failing" | "critical";

export function ConformancePage() {
  const { session, status } = useSession();
  const connected = status === "connected";

  const [summary, setSummary] = useState<RunSummary | null>(null);
  const [live, setLive] = useState<ScenarioResult[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [filter, setFilter] = useState<Filter>("all");

  const run = async () => {
    setRunning(true);
    setError(null);
    setSummary(null);
    setLive([]);
    try {
      const collected: ScenarioResult[] = [];
      const result = await runCorpus(session, ALL_SCENARIOS, {
        onResult: (r) => {
          collected.push(r);
          setLive([...collected]);
        },
      });
      setSummary(result);
    } catch (cause) {
      setError(cause);
    } finally {
      setRunning(false);
    }
  };

  const results = summary?.results ?? live;
  const visible = results.filter((r) =>
    filter === "all"
      ? true
      : filter === "failing"
        ? verdict(r) !== "pass"
        : r.scenario.critical,
  );

  const green = summary !== null && summary.failed === 0 && summary.errored === 0;

  if (!connected) {
    return (
      <EmptyState
        icon="✓"
        title="Not connected"
        detail="The suite runs against the deployed control plane with your key, which is what makes the result evidence rather than a fixture."
        action={<Button onClick={() => (window.location.hash = "/connect")}>Connect</Button>}
      />
    );
  }

  return (
    <div className="space-y-7">
      <div>
        <h1 className="text-[26px] font-semibold tracking-tight text-ink-100">
          Conformance
        </h1>
      </div>

      <Card className="p-5">
        <div className="flex flex-wrap items-center gap-4">
          <Button loading={running} disabled={running} onClick={() => void run()}>
            Run the suite
          </Button>
          {running && (
            <span className="font-mono text-[12.5px] text-brand-400">
              {live.length}/{ALL_SCENARIOS.length}
            </span>
          )}
          <span className="text-[12px] text-ink-500">
            Runs through <span className="font-mono">/v1/simulate</span>, so it writes no
            audit records.
          </span>
        </div>
        <div className="mt-4 grid gap-3 border-t border-ink-800 pt-4 sm:grid-cols-2">
          {SUITES.map((suite) => (
            <div key={suite.file} className="rounded-lg border border-ink-800 p-3">
              <div className="flex items-center gap-2">
                <span className="font-mono text-[12px] text-ink-200">{suite.file}</span>
                <Badge>{suite.scenarios.length}</Badge>
              </div>
              <p className="mt-1.5 text-[12px] leading-relaxed text-ink-500">
                {suite.description || suite.name}
              </p>
            </div>
          ))}
        </div>
      </Card>

      <ErrorNote error={error} />

      {summary && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.3 }}
        >
          <GlowCard
            className="p-6"
            accent={green ? "var(--color-allow)" : "var(--color-block)"}
          >
            {green && <ShimmerBorder />}
            <div className="flex flex-wrap items-center gap-5">
              <div>
                <div
                  className={cn(
                    "font-mono text-[30px] font-semibold",
                    green ? "text-allow" : "text-block",
                  )}
                >
                  {summary.passed}/{summary.results.length}
                </div>
                <div className="mt-1 text-[13px] text-ink-300">
                  {green ? "all scenarios passed" : "passed"}
                </div>
              </div>

              <div className="h-12 w-px bg-ink-800" />

              <div className="grid grid-cols-2 gap-x-8 gap-y-2 sm:grid-cols-4">
                <div>
                  <div className="font-mono text-[16px] text-block">{summary.failed}</div>
                  <div className="text-[11.5px] text-ink-500">failed</div>
                </div>
                <div>
                  <div className="font-mono text-[16px] text-hitl">{summary.errored}</div>
                  <div className="text-[11.5px] text-ink-500">errored</div>
                </div>
                <div>
                  <div
                    className={cn(
                      "font-mono text-[16px]",
                      summary.criticalFailures > 0 ? "text-block" : "text-ink-300",
                    )}
                  >
                    {summary.criticalFailures}
                  </div>
                  <div className="text-[11.5px] text-ink-500">critical failures</div>
                </div>
                <div>
                  <div className="font-mono text-[16px] text-ink-300">
                    {duration(summary.durationMs)}
                  </div>
                  <div className="text-[11.5px] text-ink-500">elapsed</div>
                </div>
              </div>
            </div>

            {summary.criticalFailures > 0 && (
              <p className="mt-4 rounded-lg border border-[color-mix(in_oklab,var(--color-block)_45%,transparent)] p-3 text-[13px] leading-relaxed text-block">
                {summary.criticalFailures} of the failures {summary.criticalFailures === 1 ? "is" : "are"}{" "}
                marked <strong>critical</strong> — a stated requirement of the problem
                statement, not a nice-to-have. Counted separately so a passing-looking
                summary line cannot hide one.
              </p>
            )}

            {summary.errored > 0 && (
              <p className="mt-4 text-[12.5px] leading-relaxed text-ink-400">
                An errored scenario is a request that did not complete — unreachable
                service, rate limit, bad key. It is not a policy failure, and reporting it
                as one would send somebody to edit a rule that is working correctly.
              </p>
            )}

            <div className="mt-5 flex flex-wrap gap-3 border-t border-ink-800 pt-4">
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  const report = {
                    generated_at: new Date().toISOString(),
                    endpoint: session.baseUrl,
                    passed: summary.passed,
                    failed: summary.failed,
                    errored: summary.errored,
                    critical_failures: summary.criticalFailures,
                    duration_ms: Math.round(summary.durationMs),
                    results: summary.results.map((r) => ({
                      id: r.scenario.id,
                      suite: r.scenario.suite,
                      critical: r.scenario.critical,
                      verdict: verdict(r),
                      failures: r.failures,
                      error: r.error,
                      observed: r.observed,
                    })),
                  };
                  void navigator.clipboard?.writeText(prettyJson(report));
                }}
              >
                Copy report as JSON
              </Button>
              <span className="text-[12px] text-ink-500">
                CI produces the canonical JUnit XML and HTML evidence — see below.
              </span>
            </div>
          </GlowCard>
        </motion.div>
      )}

      {results.length > 0 && (
        <div className="space-y-3">
          <Tabs
            value={filter}
            onChange={setFilter}
            tabs={[
              { id: "all", label: "All", count: results.length },
              {
                id: "failing",
                label: "Not passing",
                count: results.filter((r) => verdict(r) !== "pass").length,
              },
              {
                id: "critical",
                label: "Critical",
                count: results.filter((r) => r.scenario.critical).length,
              },
            ]}
          />
          <div className="space-y-2">
            {visible.map((result) => (
              <ResultRow key={result.scenario.id} result={result} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
