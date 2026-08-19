/**
 * Decision Theatre — send any tool call and watch policy answer it.
 *
 * Two buttons, and the difference between them is the point of the page:
 *
 * * **Evaluate** is the hot path. It writes an audit record, it can create a pending
 *   decision, and it is what an agent's SDK actually calls.
 * * **Simulate** answers the same question and writes *nothing*. That is what makes it
 *   safe to explore policy, and it is also its cost: a simulation leaves no trace in the
 *   chain, so speculation never dilutes the evidence.
 *
 * The **derived facts** panel is the one most worth looking at. Rules do not match raw
 * arguments; they match normalised facts. `record_count` is the same fact whether the
 * agent passed `count: 500`, a list of 500 ids, or a `WHERE` clause — which is why one
 * rule covers all three, and why phrasing the argument differently is not a way around it.
 */

import { motion } from "framer-motion";
import { useState } from "react";
import { api, type ActionEnvelopeInput } from "@/lib/api";
import { effectStyle, EFFECT_MEANING, prettyJson, shortHash } from "@/lib/format";
import { useSession } from "@/lib/store";
import type { Effect, EvaluateResponse, SimulateResponse } from "@/lib/types";
import { DecisionBadge } from "@/components/DecisionBadge";
import { GlowCard, VerdictGlow } from "@/components/effects";
import {
  Badge,
  Button,
  Card,
  CodeBlock,
  ErrorNote,
  Field,
  Input,
  KeyValue,
  SectionTitle,
  Select,
  Textarea,
  Toggle,
} from "@/components/ui";

/**
 * Presets covering the brief's five success criteria plus the interesting edges.
 *
 * The last two exist to make a point that a happy-path demo would hide: an argument
 * phrased to dodge a rule still gets caught, and an unparseable one fails *closed*.
 */
const PRESETS: Array<{
  label: string;
  tool: string;
  args: Record<string, unknown>;
  expect: Effect;
  why: string;
}> = [
  {
    label: "Bulk delete (500)",
    tool: "db.delete_records",
    args: { table: "users", count: 500, where: "last_login < '2024-01-01'" },
    expect: "block",
    why: "Above the blast-radius threshold of 100.",
  },
  {
    label: "Small delete (5)",
    tool: "db.delete_records",
    args: { table: "users", count: 5 },
    expect: "allow",
    why: "Under the threshold. Same tool, different blast radius.",
  },
  {
    label: "External email",
    tool: "email.send",
    args: { to: "auditor@external-firm.com", subject: "Q3 revenue" },
    expect: "require_hitl",
    why: "A recipient outside the organisation. Held for a human.",
  },
  {
    label: "Internal email",
    tool: "email.send",
    args: { to: "finance@acme-corp.com", subject: "Q3 revenue" },
    expect: "allow",
    why: "Every recipient is internal.",
  },
  {
    label: "Confidential read",
    tool: "file.read",
    args: { path: "/srv/confidential/q3.pdf" },
    expect: "log_and_allow",
    why: "Permitted, but recorded with full detail.",
  },
  {
    label: "Evasion — bcc instead of to",
    tool: "email.send",
    args: { to: "finance@acme-corp.com", bcc: "leak@external-firm.com", subject: "Q3" },
    expect: "require_hitl",
    why: "The extractor reads to, cc and bcc. Hiding the recipient does not hide it.",
  },
  {
    label: "Evasion — id list instead of a count",
    tool: "db.delete_records",
    args: {
      table: "users",
      ids: Array.from({ length: 220 }, (_, index) => 1000 + index),
    },
    expect: "block",
    why: "record_count is derived from the list. No count field is needed to be caught.",
  },
  {
    label: "Unparseable — fails closed",
    tool: "db.delete_records",
    args: { table: "users", where: "something the extractor cannot read" },
    expect: "require_hitl",
    why: "UNKNOWN resolves toward the restrictive outcome, never the permissive one.",
  },
];

function Verdict({
  result,
  mode,
}: {
  result: EvaluateResponse | SimulateResponse;
  mode: "evaluate" | "simulate";
}) {
  const style = effectStyle(result.decision);
  const accent =
    result.decision === "block"
      ? "var(--color-block)"
      : result.decision === "require_hitl"
        ? "var(--color-hitl)"
        : result.decision === "log_and_allow"
          ? "var(--color-log)"
          : "var(--color-allow)";

  const evaluated = "audit_seq" in result ? result : null;
  const simulated = "derived" in result ? (result as SimulateResponse) : null;

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35 }}
      className="space-y-4"
    >
      <GlowCard className="relative overflow-hidden p-6" accent={accent}>
        <VerdictGlow colour={accent} />
        <div className="relative">
          <div className="flex flex-wrap items-center gap-3">
            <DecisionBadge effect={result.decision} size="lg" animate />
            <Badge tone={mode === "simulate" ? "brand" : "neutral"}>
              {mode === "simulate" ? "simulated — nothing recorded" : "recorded in the chain"}
            </Badge>
            {result.allowed ? (
              <span className="text-[13px] text-ink-400">
                the caller may dispatch this now
              </span>
            ) : (
              <span className="text-[13px] text-ink-400">
                the caller must not dispatch this
              </span>
            )}
          </div>

          <p className={`mt-4 text-[15px] leading-relaxed ${style.text}`}>
            {result.message ?? EFFECT_MEANING[result.decision]}
          </p>

          <div className="mt-5 grid gap-4 border-t border-ink-800 pt-4 sm:grid-cols-2 lg:grid-cols-4">
            <KeyValue
              label="Policy"
              value={`${result.bundle_id} v${result.bundle_version}`}
              mono
            />
            <KeyValue label="Latency" value={`${result.latency_ms.toFixed(1)} ms`} mono />
            {evaluated && (
              <>
                <KeyValue label="Audit seq" value={`#${evaluated.audit_seq}`} mono />
                <KeyValue
                  label="Chain hash"
                  value={shortHash(evaluated.audit_hash)}
                  mono
                  title={evaluated.audit_hash}
                />
              </>
            )}
            {simulated && (
              <KeyValue label="Bundle source" value={simulated.bundle_source} mono />
            )}
          </div>
        </div>
      </GlowCard>

      {evaluated?.hitl && (
        <Card className="border-[color-mix(in_oklab,var(--color-hitl)_38%,transparent)] p-5">
          <div className="flex flex-wrap items-center gap-3">
            <span className="text-[13.5px] font-medium text-hitl">
              Queued for a human
            </span>
            <Badge>expires in {evaluated.hitl.timeout_seconds}s</Badge>
            <Badge tone={evaluated.hitl.on_timeout === "deny" ? "bad" : "good"}>
              on timeout: {evaluated.hitl.on_timeout}
            </Badge>
          </div>
          <p className="mt-2 text-[12.5px] leading-relaxed text-ink-400">
            The pending record was created <em>before</em> this response was returned, so
            the id you were handed is queryable immediately — a fast SDK poll can never
            404 on its first attempt.
          </p>
          <Button
            size="sm"
            className="mt-3"
            onClick={() => (window.location.hash = "/review")}
          >
            Resolve it in the review queue →
          </Button>
        </Card>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card className="p-5">
          <SectionTitle
            title="Rules that fired"
            hint="Every match, not only the winner. An action tripping four rules is worth seeing even when one dominates."
          />
          {result.matched_rules.length === 0 ? (
            <p className="text-[13px] text-ink-500">
              None. The bundle default applied instead.
            </p>
          ) : (
            <div className="space-y-2">
              {result.matched_rules.map((rule) => {
                const winner = rule.effect === result.decision;
                return (
                  <div
                    key={rule.rule_id}
                    className={`rounded-lg border p-3 ${
                      winner
                        ? "border-ink-600 bg-ink-850"
                        : "border-ink-800 bg-ink-900/40"
                    }`}
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-[13px] text-ink-100">
                        {rule.rule_id}
                      </span>
                      <DecisionBadge effect={rule.effect} size="sm" />
                      <Badge>{rule.severity}</Badge>
                      {winner && <Badge tone="brand">decided the outcome</Badge>}
                    </div>
                    {rule.message && (
                      <p className="mt-2 text-[12.5px] leading-relaxed text-ink-400">
                        {rule.message}
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </Card>

        <Card className="p-5">
          <SectionTitle
            title="Derived facts"
            hint="What the rules were actually matched against. Rules never see raw arguments, which is why re-phrasing them does not evade policy."
          />
          {simulated && Object.keys(simulated.derived).length > 0 ? (
            <div className="space-y-2">
              {Object.entries(simulated.derived).map(([key, value]) => (
                <div
                  key={key}
                  className="flex items-baseline justify-between gap-4 rounded-lg border border-ink-800 bg-ink-900/40 px-3 py-2"
                >
                  <span className="font-mono text-[12.5px] text-ink-400">
                    derived.{key}
                  </span>
                  <span className="truncate font-mono text-[12.5px] text-ink-100">
                    {typeof value === "object" ? JSON.stringify(value) : String(value)}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-[13px] leading-relaxed text-ink-500">
              Derived facts are returned by <span className="font-mono">/v1/simulate</span>
              . Run the same action in simulate mode to see them — the hot path leaves them
              out of the response deliberately, to keep the payload small on the call every
              tool dispatch makes.
            </p>
          )}

          {result.unknown_paths.length > 0 && (
            <div className="mt-4 rounded-lg border border-[color-mix(in_oklab,var(--color-hitl)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-hitl)_8%,transparent)] p-3">
              <div className="text-[12px] font-semibold uppercase tracking-wider text-hitl">
                Unknown paths
              </div>
              <div className="mt-1.5 font-mono text-[12px] text-ink-300">
                {result.unknown_paths.join(", ")}
              </div>
              <p className="mt-2 text-[12.5px] leading-relaxed text-ink-400">
                An extractor could not resolve these, so the decision failed{" "}
                <strong className="text-ink-200">closed</strong>. Surfaced rather than
                hidden: repeated unknowns mean policy is being applied more conservatively
                than its author intended.
              </p>
            </div>
          )}
        </Card>
      </div>
    </motion.div>
  );
}

export function DecisionTheatrePage() {
  const { session, status } = useSession();
  const [preset, setPreset] = useState(0);
  const [tool, setTool] = useState(PRESETS[0].tool);
  const [argsText, setArgsText] = useState(prettyJson(PRESETS[0].args));
  const [agentId, setAgentId] = useState("console-operator");
  const [dryRun, setDryRun] = useState(false);
  const [busy, setBusy] = useState<null | "evaluate" | "simulate">(null);
  const [mode, setMode] = useState<"evaluate" | "simulate">("evaluate");
  const [result, setResult] = useState<EvaluateResponse | SimulateResponse | null>(null);
  const [error, setError] = useState<unknown>(null);

  const applyPreset = (index: number) => {
    setPreset(index);
    setTool(PRESETS[index].tool);
    setArgsText(prettyJson(PRESETS[index].args));
    setResult(null);
    setError(null);
  };

  const buildAction = (): ActionEnvelopeInput => ({
    agent_id: agentId || "console-operator",
    session_id: `console-${Date.now().toString(36)}`,
    tool,
    arguments: JSON.parse(argsText) as Record<string, unknown>,
    dry_run: dryRun,
  });

  const send = async (which: "evaluate" | "simulate") => {
    setBusy(which);
    setError(null);
    setResult(null);
    try {
      const action = buildAction();
      setMode(which);
      setResult(
        which === "evaluate"
          ? await api.evaluate(session, action)
          : await api.simulate(session, action),
      );
    } catch (cause) {
      setError(
        cause instanceof SyntaxError
          ? new Error(`Arguments are not valid JSON: ${cause.message}`)
          : cause,
      );
    } finally {
      setBusy(null);
    }
  };

  const disconnected = status !== "connected";

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-[26px] font-semibold tracking-tight text-ink-100">
          Decision Theatre
        </h1>
        <p className="mt-2 max-w-3xl text-[14px] leading-relaxed text-ink-400">
          Send a tool call directly to the deployed policy engine and read the verdict.
          The decision path is deterministic and never calls an LLM — the same envelope
          gets the same answer every time, which is what makes policy testable and what
          makes it immune to prompt injection.
        </p>
      </div>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,0.95fr)_minmax(0,1.05fr)]">
        <Card className="p-5">
          <SectionTitle title="Compose an action" />

          <div className="mb-4 flex flex-wrap gap-2">
            {PRESETS.map((entry, index) => (
              <button
                key={entry.label}
                type="button"
                onClick={() => applyPreset(index)}
                className={`rounded-lg border px-3 py-1.5 text-[12.5px] transition-colors ${
                  preset === index
                    ? "border-brand-600 bg-brand-500/10 text-brand-400"
                    : "border-ink-700 text-ink-400 hover:border-ink-600 hover:text-ink-200"
                }`}
              >
                {entry.label}
              </button>
            ))}
          </div>

          <p className="mb-4 rounded-lg border border-ink-800 bg-ink-900/40 p-3 text-[12.5px] leading-relaxed text-ink-400">
            <span className="font-mono text-ink-200">
              expects {PRESETS[preset].expect}
            </span>{" "}
            — {PRESETS[preset].why}
          </p>

          <div className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Tool">
                <Select value={tool} onChange={(event) => setTool(event.target.value)}>
                  {[
                    "db.delete_records",
                    "email.send",
                    "file.read",
                    "http.request",
                    "payments.refund",
                  ].map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Agent id" hint="Recorded against the decision.">
                <Input
                  value={agentId}
                  onChange={(event) => setAgentId(event.target.value)}
                  className="font-mono text-[12.5px]"
                />
              </Field>
            </div>

            <Field
              label="Arguments (JSON)"
              hint="Exactly what the model would have produced. Edit freely — try to get a blocked action past the rule."
            >
              <Textarea
                rows={10}
                value={argsText}
                onChange={(event) => setArgsText(event.target.value)}
              />
            </Field>

            <Toggle
              checked={dryRun}
              onChange={setDryRun}
              label="dry_run"
              hint="The caller promises not to execute regardless of the verdict. The engine still evaluates and still records — flagged, and excluded from enforcement metrics."
            />

            <div className="flex flex-wrap gap-3 pt-1">
              <Button
                onClick={() => void send("evaluate")}
                loading={busy === "evaluate"}
                disabled={disconnected || busy !== null}
              >
                Evaluate
              </Button>
              <Button
                variant="outline"
                onClick={() => void send("simulate")}
                loading={busy === "simulate"}
                disabled={disconnected || busy !== null}
              >
                Simulate
              </Button>
            </div>

            <p className="text-[12.5px] leading-relaxed text-ink-500">
              <strong className="text-ink-300">Evaluate</strong> is the hot path — it
              writes to the audit chain and can queue a human review.{" "}
              <strong className="text-ink-300">Simulate</strong> answers the same question
              and records nothing, which also means it leaves no trace: exploration never
              dilutes the evidence.
            </p>

            {disconnected && (
              <p className="text-[13px] text-hitl">
                Connect a key first.{" "}
                <a className="underline" href="#/connect">
                  Connection settings
                </a>
              </p>
            )}
          </div>
        </Card>

        <div className="space-y-4">
          <ErrorNote error={error} />
          {result ? (
            <Verdict result={result} mode={mode} />
          ) : (
            !error && (
              <Card className="flex min-h-[280px] flex-col items-center justify-center p-8 text-center">
                <div className="mb-4 flex gap-2">
                  {(["allow", "log_and_allow", "require_hitl", "block"] as Effect[]).map(
                    (effect) => (
                      <DecisionBadge key={effect} effect={effect} size="sm" />
                    ),
                  )}
                </div>
                <p className="text-[13.5px] text-ink-300">
                  One of these four comes back, with the rules that produced it.
                </p>
                <p className="mt-2 max-w-md text-[12.5px] leading-relaxed text-ink-500">
                  Resolution is most-restrictive-wins rather than first-match, so
                  reordering the policy file cannot silently weaken it — a property that
                  first-match policy engines do not have.
                </p>
              </Card>
            )
          )}

          {result && (
            <details className="group">
              <summary className="cursor-pointer list-none text-[12.5px] text-ink-500 transition-colors hover:text-ink-300">
                <span className="inline-block transition-transform group-open:rotate-90">
                  ▸
                </span>{" "}
                raw response
              </summary>
              <CodeBlock className="mt-2" code={prettyJson(result)} />
            </details>
          )}
        </div>
      </div>
    </div>
  );
}
