/**
 * The audit log, and the thing that separates it from a log file.
 *
 * Anyone can append to a log. The chain is what makes a silent edit detectable:
 *
 *     hash = sha256(prev_hash ‖ canonical_json(payload))
 *
 * so record *n* commits to record *n-1*, and editing one record breaks every link after
 * it. `/v1/audit/verify` walks the whole chain and reports not just whether it is intact
 * but *how* it broke — a sequence gap means a deleted record, a broken link means a
 * reordered or substituted one, and a content mismatch means an edited one. Those imply
 * different incidents.
 *
 * ## What this page must not overclaim
 *
 * The chain is **tamper-evident, not tamper-proof**. Someone with table-wide write access
 * could recompute every hash from a forged record onward and produce an internally
 * consistent history; detecting that needs an anchor outside their control, which is not
 * implemented. It defends against selective tampering, which is the realistic insider
 * case. That limitation is stated on the page itself rather than left in a document
 * nobody opens.
 */

import { motion } from "framer-motion";
import { useMemo, useState } from "react";
import { api } from "@/lib/api";
import { effectStyle, prettyJson, relativeTime, shortHash } from "@/lib/format";
import { useAsync, useSession } from "@/lib/store";
import type { AuditEntry, Effect } from "@/lib/types";
import { DecisionBadge } from "@/components/DecisionBadge";
import { GlowCard, ShimmerBorder } from "@/components/effects";
import {
  Badge,
  Button,
  Card,
  CodeBlock,
  EmptyState,
  ErrorNote,
  KeyValue,
  SectionTitle,
  Select,
  Skeleton,
} from "@/components/ui";

function ChainVerdict() {
  const { session, status } = useSession();
  const connected = status === "connected";
  const { data, error, loading, reload } = useAsync(
    () => (connected ? api.verify(session) : Promise.resolve(null)),
    [connected, session.baseUrl, session.apiKey],
  );

  if (error) return <ErrorNote error={error} />;
  if (loading || !data) return <Skeleton className="h-32 w-full" />;

  const valid = data.chain_valid;
  const accent = valid ? "var(--color-allow)" : "var(--color-block)";

  return (
    <GlowCard className="overflow-hidden p-6" accent={accent}>
      {valid && <ShimmerBorder />}
      <div className="flex flex-wrap items-start justify-between gap-5">
        <div className="min-w-0">
          <div className="flex items-center gap-3">
            <motion.span
              initial={{ scale: 0.6, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              transition={{ type: "spring", stiffness: 380, damping: 22 }}
              className="flex h-10 w-10 items-center justify-center rounded-full"
              style={{
                background: `color-mix(in oklab, ${accent} 18%, transparent)`,
                border: `1px solid color-mix(in oklab, ${accent} 50%, transparent)`,
              }}
            >
              <span style={{ color: accent }} className="text-[16px]">
                {valid ? "✓" : "✕"}
              </span>
            </motion.span>
            <div>
              <div
                className="text-[19px] font-semibold tracking-tight"
                style={{ color: accent }}
              >
                {valid ? "Chain verified intact" : "CHAIN BROKEN"}
              </div>
              <div className="mt-0.5 font-mono text-[12.5px] text-ink-400">
                {data.records_checked} records recomputed · tenant {data.tenant_id}
              </div>
            </div>
          </div>

          {!valid && (
            <div className="mt-4 rounded-lg border border-[color-mix(in_oklab,var(--color-block)_40%,transparent)] bg-[color-mix(in_oklab,var(--color-block)_8%,transparent)] p-3.5">
              <div className="font-mono text-[12px] text-block">
                broke at seq {data.broken_at_seq ?? "?"}
              </div>
              <p className="mt-1.5 text-[13px] leading-relaxed text-ink-200">
                {data.reason}
              </p>
            </div>
          )}
        </div>
        <Button size="sm" variant="outline" onClick={reload}>
          Re-verify
        </Button>
      </div>

      <p className="mt-5 border-t border-ink-800 pt-4 text-[12.5px] leading-relaxed text-ink-500">
        Every record's hash is recomputed from its predecessor and its canonical JSON
        payload. Payloads are stored as canonical JSON <em>strings</em> rather than
        decoded numbers, deliberately: DynamoDB rejects Python floats, and converting to
        Decimal risks a non-identical round trip — which would raise a{" "}
        <strong className="text-ink-300">false tamper alarm</strong>, worse than a missed
        one.
      </p>
    </GlowCard>
  );
}

function ChainLink({ entry, previous }: { entry: AuditEntry; previous?: AuditEntry }) {
  const [open, setOpen] = useState(false);
  const style = effectStyle(entry.effect);
  const linked = previous ? previous.hash === entry.prev_hash : true;

  return (
    <div className="relative pl-8">
      {/* The literal link between records. */}
      <span
        className={`absolute left-[11px] top-0 h-full w-px ${
          linked ? "bg-ink-700" : "bg-block"
        }`}
      />
      <span
        className={`absolute left-[5px] top-6 h-3 w-3 rounded-full border-2 border-ink-950 ${style.dot}`}
      />

      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="w-full rounded-xl border border-ink-800 bg-ink-900/40 p-4 text-left transition-colors hover:border-ink-700 hover:bg-ink-900/70"
      >
        <div className="flex flex-wrap items-center gap-3">
          <span className="font-mono text-[12px] text-ink-500">#{entry.seq}</span>
          <span className="font-mono text-[13.5px] font-medium text-ink-100">
            {entry.tool}
          </span>
          <DecisionBadge effect={entry.effect} size="sm" />
          {entry.dry_run && <Badge tone="brand">dry run</Badge>}
          <span className="ml-auto font-mono text-[11.5px] text-ink-500">
            {relativeTime(entry.timestamp)}
          </span>
        </div>

        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[11.5px] text-ink-500">
          <span title={entry.prev_hash}>prev {shortHash(entry.prev_hash, 8)}</span>
          <span className={linked ? "text-ink-600" : "text-block"}>→</span>
          <span className="text-ink-400" title={entry.hash}>
            {shortHash(entry.hash, 8)}
          </span>
          <span className="text-ink-600">·</span>
          <span>{entry.agent_id}</span>
          {entry.latency_ms !== null && (
            <>
              <span className="text-ink-600">·</span>
              <span>{entry.latency_ms.toFixed(1)} ms</span>
            </>
          )}
          <span className="text-ink-600">·</span>
          <span>
            policy {entry.bundle_id} v{entry.bundle_version}
          </span>
        </div>

        {entry.message && (
          <p className="mt-2 text-[12.5px] leading-relaxed text-ink-400">{entry.message}</p>
        )}

        {!linked && (
          <p className="mt-2 font-mono text-[12px] text-block">
            link mismatch — this record's prev_hash does not match the record before it
          </p>
        )}
      </button>

      {open && (
        <motion.div
          initial={{ opacity: 0, height: 0 }}
          animate={{ opacity: 1, height: "auto" }}
          className="overflow-hidden"
        >
          <div className="mt-2 grid gap-3 rounded-xl border border-ink-800 bg-ink-950/50 p-4 lg:grid-cols-2">
            <div>
              <div className="mb-2 text-[11px] uppercase tracking-wider text-ink-500">
                Arguments evaluated
              </div>
              <CodeBlock maxHeight="16rem" code={prettyJson(entry.arguments)} />
            </div>
            <div className="space-y-3">
              <div>
                <div className="mb-2 text-[11px] uppercase tracking-wider text-ink-500">
                  Derived facts
                </div>
                <CodeBlock maxHeight="8rem" code={prettyJson(entry.derived)} />
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                <KeyValue label="Decision id" value={entry.decision_id.slice(0, 18)} mono />
                <KeyValue label="Session" value={entry.session_id.slice(0, 18)} mono />
              </div>
              {entry.matched_rules.length > 0 && (
                <div>
                  <div className="mb-1.5 text-[11px] uppercase tracking-wider text-ink-500">
                    Rules matched
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {entry.matched_rules.map((rule, index) => (
                      <Badge key={index} tone="brand">
                        {String(rule.rule_id ?? "rule")}
                      </Badge>
                    ))}
                  </div>
                </div>
              )}
              {entry.unknown_paths.length > 0 && (
                <div>
                  <div className="mb-1.5 text-[11px] uppercase tracking-wider text-hitl">
                    Unknown paths — failed closed
                  </div>
                  <div className="font-mono text-[12px] text-ink-300">
                    {entry.unknown_paths.join(", ")}
                  </div>
                </div>
              )}
              <div>
                <div className="mb-1.5 text-[11px] uppercase tracking-wider text-ink-500">
                  Full hash
                </div>
                <div className="break-all font-mono text-[11.5px] text-ink-400">
                  {entry.hash}
                </div>
              </div>
            </div>
          </div>
        </motion.div>
      )}
    </div>
  );
}

export function AuditChainPage() {
  const { session, status } = useSession();
  const connected = status === "connected";
  const [filter, setFilter] = useState<Effect | "">("");
  const [limit, setLimit] = useState(50);

  const log = useAsync(
    () =>
      connected
        ? api.audit(session, limit, filter || undefined)
        : Promise.resolve(null),
    [connected, session.baseUrl, session.apiKey, filter, limit],
  );

  const entries = useMemo(() => log.data?.entries ?? [], [log.data]);

  // Returned newest-first; the chain reads oldest-first, since each record commits to
  // the one before it.
  const ordered = useMemo(() => [...entries].reverse(), [entries]);

  if (!connected) {
    return (
      <EmptyState
        icon="⛓"
        title="Not connected"
        detail="The audit log is scoped to your tenant from the API key rather than a query parameter, so one tenant cannot read another's by editing a URL."
        action={<Button onClick={() => (window.location.hash = "/connect")}>Connect</Button>}
      />
    );
  }

  return (
    <div className="space-y-7">
      <div>
        <h1 className="text-[26px] font-semibold tracking-tight text-ink-100">
          Audit & Chain
        </h1>
        <p className="mt-2 max-w-3xl text-[14px] leading-relaxed text-ink-400">
          Every decision is recorded — allows included, not only refusals. The record
          holds the arguments evaluated, the facts derived from them, the rules matched,
          the exact policy version in force, and the latency. A decision can therefore be
          reproduced later rather than re-litigated.
        </p>
      </div>

      <ChainVerdict />

      <div className="flex flex-wrap items-end gap-3">
        <div className="w-44">
          <Select
            value={filter}
            onChange={(event) => setFilter(event.target.value as Effect | "")}
          >
            <option value="">All outcomes</option>
            <option value="block">block</option>
            <option value="require_hitl">require_hitl</option>
            <option value="log_and_allow">log_and_allow</option>
            <option value="allow">allow</option>
          </Select>
        </div>
        <div className="w-32">
          <Select
            value={String(limit)}
            onChange={(event) => setLimit(Number(event.target.value))}
          >
            {[25, 50, 100, 200].map((value) => (
              <option key={value} value={value}>
                last {value}
              </option>
            ))}
          </Select>
        </div>
        <Button size="sm" variant="outline" onClick={log.reload}>
          Refresh
        </Button>
        <span className="ml-auto font-mono text-[12px] text-ink-500">
          {log.data?.count ?? 0} record{(log.data?.count ?? 0) === 1 ? "" : "s"} · tenant{" "}
          {log.data?.tenant_id}
        </span>
      </div>

      <ErrorNote error={log.error} />

      {log.loading ? (
        <div className="space-y-3">
          {[0, 1, 2, 3].map((index) => (
            <Skeleton key={index} className="h-24 w-full" />
          ))}
        </div>
      ) : ordered.length === 0 ? (
        <EmptyState
          icon="⛓"
          title="No records yet"
          detail="Send a tool call from Decision Theatre, or run the agent — every decision it makes lands here, including the ones that were allowed."
          action={
            <Button onClick={() => (window.location.hash = "/theatre")}>
              Send a tool call
            </Button>
          }
        />
      ) : (
        <div className="space-y-2">
          {ordered.map((entry, index) => (
            <ChainLink
              key={`${entry.seq}-${entry.hash}`}
              entry={entry}
              previous={index > 0 ? ordered[index - 1] : undefined}
            />
          ))}
        </div>
      )}

      <Card className="p-5">
        <SectionTitle title="What this chain does and does not prove" />
        <div className="grid gap-4 lg:grid-cols-2">
          <div>
            <div className="mb-2 font-mono text-[12px] uppercase tracking-wider text-allow">
              detected
            </div>
            <ul className="space-y-1.5 text-[13px] leading-relaxed text-ink-300">
              <li>· Editing one record — the recomputed hash no longer matches</li>
              <li>· Deleting a record from the middle — a sequence gap</li>
              <li>· Reordering records — a broken prev_hash link</li>
            </ul>
          </div>
          <div>
            <div className="mb-2 font-mono text-[12px] uppercase tracking-wider text-block">
              not detected
            </div>
            <ul className="space-y-1.5 text-[13px] leading-relaxed text-ink-300">
              <li>
                · A consistent rewrite of the whole chain by someone with table-wide write
                access. Catching that needs an anchor outside their control — publishing
                the head hash to another account or a third party.{" "}
                <span className="text-ink-500">Not implemented, and named as gap 1.</span>
              </li>
              <li>
                · Deletion of the entire table. Point-in-time recovery is enabled, and the
                service's own IAM role has no{" "}
                <span className="font-mono">DeleteItem</span> — it cannot erase its own
                evidence.
              </li>
            </ul>
          </div>
        </div>
      </Card>
    </div>
  );
}
