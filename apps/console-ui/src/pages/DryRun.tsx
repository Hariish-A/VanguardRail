/**
 * Dry-run & Shadow — evaluating without enforcing, at three levels.
 *
 * The problem statement's bonus asks for a dry-run mode. There are three here, and they
 * are genuinely different things rather than one feature described three ways:
 *
 * | | Executes the tool | Writes an audit record | Can hold a decision |
 * |---|---|---|---|
 * | `dry_run: true` on a request | no | **yes**, tagged | no |
 * | `mode: shadow` on the bundle | yes | yes | no — block and hitl are downgraded |
 * | `POST /v1/simulate` | no | **no** | no |
 *
 * ## Why the parity run uses the enforcement path, and writes
 *
 * The claim being tested is "dry-run reports what enforcement would really do". Testing
 * that through `/v1/simulate` would prove nothing about the enforcement path, because
 * simulation is a different code path by construction. So this page calls `/v1/evaluate`
 * twice per scenario — once with `dry_run: true`, once without — and both writes land in
 * the audit chain.
 *
 * That is a real cost, stated on the page: a parity run adds two records per scenario to
 * a tamper-evident log, against a table provisioned at 5 write units. The default subset
 * is the five success criteria, not the full corpus, for exactly that reason.
 */

import { useState } from "react";
import { api } from "@/lib/api";
import { ALL_SCENARIOS } from "@/lib/conformance";
import { cn } from "@/lib/format";
import { useAsync, useSession } from "@/lib/store";
import type { EvaluateResponse } from "@/lib/types";
import { DecisionBadge } from "@/components/DecisionBadge";
import { GlowCard } from "@/components/effects";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorNote,
  KeyValue,
  SectionTitle,
  Skeleton,
  Toggle,
} from "@/components/ui";

interface ParityRow {
  id: string;
  tool: string;
  dry: EvaluateResponse | null;
  live: EvaluateResponse | null;
  agrees: boolean | null;
  error: string | null;
}

export function DryRunPage() {
  const { session, status } = useSession();
  const connected = status === "connected";

  const active = useAsync(
    () => (connected ? api.activePolicy(session) : Promise.resolve(null)),
    [connected, session.baseUrl, session.apiKey],
  );

  const [fullCorpus, setFullCorpus] = useState(false);
  const [rows, setRows] = useState<ParityRow[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [auditBefore, setAuditBefore] = useState<number | null>(null);
  const [auditAfter, setAuditAfter] = useState<number | null>(null);

  const chosen = fullCorpus
    ? ALL_SCENARIOS
    : ALL_SCENARIOS.filter((s) => s.critical && s.suite.includes("success"));

  const run = async () => {
    setRunning(true);
    setError(null);
    setRows([]);
    setAuditAfter(null);

    try {
      const before = await api.audit(session, 1);
      setAuditBefore(before.entries[0]?.seq ?? 0);
    } catch {
      setAuditBefore(null);
    }

    const collected: ParityRow[] = [];
    for (const [index, scenario] of chosen.entries()) {
      const base = {
        agent_id: "console-parity",
        session_id: `parity-${index}`,
        tool: scenario.action.tool,
        arguments: scenario.action.arguments,
      };
      try {
        const dry = await api.evaluate(session, { ...base, dry_run: true });
        const live = await api.evaluate(session, { ...base, dry_run: false });
        collected.push({
          id: scenario.id,
          tool: scenario.action.tool,
          dry,
          live,
          agrees: dry.decision === live.decision && dry.allowed === live.allowed,
          error: null,
        });
      } catch (cause) {
        collected.push({
          id: scenario.id,
          tool: scenario.action.tool,
          dry: null,
          live: null,
          agrees: null,
          error: cause instanceof Error ? cause.message : String(cause),
        });
      }
      setRows([...collected]);
    }

    try {
      const after = await api.audit(session, 1);
      setAuditAfter(after.entries[0]?.seq ?? 0);
    } catch {
      setAuditAfter(null);
    }

    setRunning(false);
  };

  const disagreements = rows.filter((r) => r.agrees === false);
  const errored = rows.filter((r) => r.error !== null);
  const shadow = active.data?.mode === "shadow";

  if (!connected) {
    return (
      <EmptyState
        icon="◐"
        title="Not connected"
        detail="A parity run calls the enforcement path, which is authenticated and tenant-scoped."
        action={<Button onClick={() => (window.location.hash = "/connect")}>Connect</Button>}
      />
    );
  }

  return (
    <div className="space-y-7">
      <div>
        <h1 className="text-[26px] font-semibold tracking-tight text-ink-100">
          Dry-run & Shadow
        </h1>
        <p className="mt-1.5 text-[13.5px] text-ink-400">
          Evaluate without enforcing, and verify that dry-run agrees with
          enforcement.
        </p>
      </div>

      {/* --------------------------------------------------------- live state */}
      <GlowCard className="p-5" accent={shadow ? "var(--color-hitl)" : "var(--color-allow)"}>
        <SectionTitle title="The policy in force right now" />
        {active.loading ? (
          <Skeleton className="h-16 w-full" />
        ) : active.error ? (
          <ErrorNote error={active.error} />
        ) : active.data ? (
          <div className="grid gap-4 sm:grid-cols-3">
            <KeyValue label="Version" value={`v${active.data.version}`} mono />
            <KeyValue
              label="Mode"
              value={
                <span className={shadow ? "font-mono text-hitl" : "font-mono text-allow"}>
                  {active.data.mode}
                </span>
              }
            />
            <KeyValue label="Rules" value={String(active.data.rule_count)} mono />
            <div className="sm:col-span-3">
              <p className="text-[12.5px] leading-relaxed text-ink-400">
                {shadow ? (
                  <>
                    <strong className="text-hitl">Shadow.</strong> Every decision is being
                    recorded, and <strong>nothing is being restrained</strong> — block and
                    require_hitl are downgraded to log_and_allow before they reach the
                    caller. Agents are being observed, not governed.
                  </>
                ) : (
                  <>
                    <strong className="text-allow">Enforce.</strong> Decisions restrain
                    callers. A blocked action does not run.
                  </>
                )}
              </p>
            </div>
          </div>
        ) : null}
      </GlowCard>

      {/* ------------------------------------------------------------ the three */}

      {/* ------------------------------------------------------------- parity */}
      <Card className="p-5">
        <SectionTitle
          title="Parity run"
          hint="Sends each action twice — dry_run true, then false — and requires the decisions to agree."
        />

        <div className="rounded-lg border border-[color-mix(in_oklab,var(--color-hitl)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-hitl)_7%,transparent)] p-3.5">
          <p className="text-[12.5px] leading-relaxed text-ink-300">
            <strong className="text-hitl">This writes to the audit chain</strong> — two
            records per action — and that is the point. Running parity through{" "}
            <span className="font-mono">/v1/simulate</span> would prove nothing about the
            enforcement path, because simulation is a different code path by construction.
            The table is provisioned at 5 write units, so the default subset is the five
            success criteria rather than the full corpus.
          </p>
        </div>

        <div className="mt-4">
          <Toggle
            checked={fullCorpus}
            onChange={setFullCorpus}
            label={`Run the full corpus (${ALL_SCENARIOS.length} actions, ${ALL_SCENARIOS.length * 2} audit records)`}
            hint={`Off: the ${chosen.length} problem-statement criteria only.`}
          />
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <Button loading={running} disabled={running} onClick={() => void run()}>
            Run parity over {chosen.length} action{chosen.length === 1 ? "" : "s"}
          </Button>
          {rows.length > 0 && (
            <span className="font-mono text-[12px] text-ink-500">
              {rows.length}/{chosen.length}
            </span>
          )}
        </div>
      </Card>

      <ErrorNote error={error} />

      {rows.length > 0 && (
        <>
          <GlowCard
            className="p-5"
            accent={
              disagreements.length > 0 ? "var(--color-block)" : "var(--color-allow)"
            }
          >
            <div className="flex flex-wrap items-center gap-4">
              <div>
                <div
                  className={cn(
                    "text-[19px] font-semibold",
                    disagreements.length > 0 ? "text-block" : "text-allow",
                  )}
                >
                  {disagreements.length > 0
                    ? `${disagreements.length} disagreement${disagreements.length === 1 ? "" : "s"}`
                    : "Dry run and enforcement agree on every action"}
                </div>
                <div className="mt-1 text-[12.5px] text-ink-400">
                  {disagreements.length > 0
                    ? "A dry run that reports a different decision than enforcement is worse than no dry run — it is a confident wrong answer."
                    : "So a dry-run report can be trusted to say what enforcement would really do."}
                </div>
              </div>
              {errored.length > 0 && (
                <Badge tone="bad">{errored.length} request errors</Badge>
              )}
            </div>

            {auditBefore !== null && auditAfter !== null && (
              <div className="mt-4 border-t border-ink-800 pt-4">
                <p className="text-[12.5px] leading-relaxed text-ink-400">
                  Audit sequence moved{" "}
                  <span className="font-mono text-ink-200">
                    {auditBefore} → {auditAfter}
                  </span>{" "}
                  ({auditAfter - auditBefore} records). Both the dry runs and the enforced
                  runs are in the chain; the dry ones carry{" "}
                  <span className="font-mono">dry_run: true</span> and are excluded from the
                  enforcement metrics, so a shadow trial cannot distort the dashboards.
                </p>
                <Button
                  size="sm"
                  variant="ghost"
                  className="mt-2"
                  onClick={() => (window.location.hash = "/audit")}
                >
                  See them in the chain →
                </Button>
              </div>
            )}
          </GlowCard>

          <div className="space-y-2">
            {rows.map((row) => (
              <Card
                key={row.id}
                className={cn(
                  "p-4",
                  row.agrees === false &&
                    "border-[color-mix(in_oklab,var(--color-block)_45%,transparent)]",
                )}
              >
                <div className="flex flex-wrap items-center gap-3">
                  <span className="font-mono text-[12.5px] text-ink-100">{row.tool}</span>
                  <span className="font-mono text-[11px] text-ink-500">{row.id}</span>
                  {row.error ? (
                    <span className="text-[12.5px] text-block">{row.error}</span>
                  ) : (
                    <>
                      <span className="ml-auto text-[11px] uppercase tracking-wider text-ink-500">
                        dry
                      </span>
                      <DecisionBadge effect={row.dry?.decision ?? "allow"} size="sm" />
                      <span className="text-[11px] uppercase tracking-wider text-ink-500">
                        enforced
                      </span>
                      <DecisionBadge effect={row.live?.decision ?? "allow"} size="sm" />
                      <span
                        className={cn(
                          "font-mono text-[11px]",
                          row.agrees ? "text-allow" : "text-block",
                        )}
                      >
                        {row.agrees ? "agree" : "DIFFER"}
                      </span>
                    </>
                  )}
                </div>
                {row.dry && row.live && (
                  <div className="mt-2 font-mono text-[11px] text-ink-600">
                    seq {row.dry.audit_seq} (dry_run: {String(row.dry.dry_run)}) · seq{" "}
                    {row.live.audit_seq} (dry_run: {String(row.live.dry_run)})
                  </div>
                )}
              </Card>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
