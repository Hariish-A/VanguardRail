/**
 * The review queue — where `require_hitl` stops being a word and becomes a person.
 *
 * ## Why this page checks a capability before rendering a button
 *
 * Resolving needs the `reviewer` role. A key with the `agent` role can *see* this queue —
 * that is how an agent reports its own status, and it is harmless — but cannot act on it.
 * The server enforces that on every request; this page reads `/v1/me` so it can explain
 * the refusal up front rather than after a 403.
 *
 * That check exists because of a defect in this system, found and verified live: before
 * roles, any valid key could resolve, so the agent whose action was held could approve it
 * with its own key. The audit chain recorded `reviewer: the-agent-itself`.
 *
 * ## Why the countdown matters
 *
 * A held decision has a deadline, and `on_timeout` is `deny` by default. Timing out is
 * therefore a *decision*, not a lapse — and a reviewer needs to see it coming.
 */

import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { cn, formatClock, prettyJson, relativeTime } from "@/lib/format";
import { useAsync, useSession } from "@/lib/store";
import type { DecisionView } from "@/lib/types";
import { StatusBadge } from "@/components/DecisionBadge";
import { GlowCard, ShimmerBorder } from "@/components/effects";
import {
  Badge,
  Button,
  Card,
  CodeBlock,
  EmptyState,
  ErrorNote,
  Field,
  Input,
  KeyValue,
  SectionTitle,
  Skeleton,
  Tabs,
} from "@/components/ui";

/** A live countdown to the decision's deadline. */
function Countdown({ decision }: { decision: DecisionView }) {
  const [remaining, setRemaining] = useState(decision.seconds_remaining);

  useEffect(() => {
    setRemaining(decision.seconds_remaining);
    if (decision.status !== "pending") return;
    const timer = window.setInterval(
      () => setRemaining((value) => Math.max(0, value - 1)),
      1000,
    );
    return () => window.clearInterval(timer);
  }, [decision.decision_id, decision.seconds_remaining, decision.status]);

  if (decision.status !== "pending") return null;

  const urgent = remaining < 120;
  const share = Math.max(
    0,
    Math.min(1, remaining / Math.max(1, decision.seconds_remaining || 1)),
  );

  return (
    <div className="min-w-[132px]">
      <div className="flex items-baseline justify-between gap-2">
        <span
          className={cn(
            "font-mono text-[15px] font-semibold tabular-nums",
            urgent ? "text-block" : "text-hitl",
          )}
        >
          {formatClock(remaining)}
        </span>
        <span className="text-[11px] uppercase tracking-wider text-ink-500">
          → {decision.on_timeout}
        </span>
      </div>
      <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-ink-800">
        <motion.div
          className={cn(
            "h-full rounded-full",
            urgent ? "bg-block" : "bg-hitl",
          )}
          initial={false}
          animate={{ width: `${share * 100}%` }}
          transition={{ duration: 0.9, ease: "linear" }}
        />
      </div>
    </div>
  );
}

function DecisionCard({
  decision,
  canResolve,
  onResolved,
}: {
  decision: DecisionView;
  canResolve: boolean;
  onResolved: (next: DecisionView) => void;
}) {
  const { session, identity } = useSession();
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<null | "approve" | "deny">(null);
  const [error, setError] = useState<unknown>(null);

  const settle = async (approve: boolean) => {
    setBusy(approve ? "approve" : "deny");
    setError(null);
    try {
      onResolved(
        await api.resolve(
          session,
          decision.decision_id,
          approve,
          reason || (approve ? "approved from console" : "denied from console"),
          identity?.name,
        ),
      );
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(null);
    }
  };

  const pending = decision.status === "pending";

  return (
    <GlowCard
      className={cn("p-5", pending && "border-[color-mix(in_oklab,var(--color-hitl)_40%,transparent)]")}
      accent={pending ? "var(--color-hitl)" : "var(--color-ink-500)"}
    >
      {pending && <ShimmerBorder />}

      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2.5">
            <span className="font-mono text-[15px] font-semibold text-ink-100">
              {decision.tool}
            </span>
            <StatusBadge status={decision.status} />
            <Badge>seq {decision.audit_seq}</Badge>
          </div>
          <div className="mt-1.5 font-mono text-[12px] text-ink-500">
            {decision.agent_id} · session {decision.session_id.slice(0, 18)} ·{" "}
            {relativeTime(decision.created_at)}
          </div>
        </div>
        <Countdown decision={decision} />
      </div>

      {decision.message && (
        <p className="mt-4 rounded-lg border border-ink-700/60 bg-ink-900/50 p-3 text-[13px] leading-relaxed text-ink-200">
          {decision.message}
        </p>
      )}

      {decision.matched_rules.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <span className="text-[12px] text-ink-500">held by:</span>
          {decision.matched_rules.map((rule, index) => (
            <Badge key={index} tone="brand">
              {String(rule.rule_id ?? "rule")}
            </Badge>
          ))}
        </div>
      )}

      <details className="group mt-4">
        <summary className="cursor-pointer list-none text-[12.5px] text-ink-500 transition-colors hover:text-ink-300">
          <span className="inline-block transition-transform group-open:rotate-90">▸</span>{" "}
          the full action being approved
        </summary>
        <p className="mt-2 text-[12.5px] leading-relaxed text-ink-500">
          A reviewer approving something they cannot see is a rubber stamp. Everything the
          agent asked for is here, unmodified.
        </p>
        <CodeBlock className="mt-2" maxHeight="16rem" code={prettyJson(decision.arguments)} />
      </details>

      {decision.status !== "pending" && (
        <div className="mt-4 grid gap-4 border-t border-ink-800 pt-4 sm:grid-cols-3">
          <KeyValue label="Reviewer" value={decision.reviewer ?? "—"} mono />
          <KeyValue
            label="Resolved"
            value={decision.resolved_at ? relativeTime(decision.resolved_at) : "—"}
          />
          <KeyValue label="Reason" value={decision.reason || "—"} />
        </div>
      )}

      {pending && (
        <div className="mt-4 border-t border-ink-800 pt-4">
          {canResolve ? (
            <>
              <Field
                label="Reason"
                hint="Recorded in the audit chain. “Who approved this” is only half the question an auditor asks."
              >
                <Input
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                  placeholder="why you are approving or denying this"
                />
              </Field>
              <div className="mt-3 flex flex-wrap gap-3">
                <Button
                  variant="approve"
                  loading={busy === "approve"}
                  disabled={busy !== null}
                  onClick={() => void settle(true)}
                >
                  Approve — let it run
                </Button>
                <Button
                  variant="danger"
                  loading={busy === "deny"}
                  disabled={busy !== null}
                  onClick={() => void settle(false)}
                >
                  Deny
                </Button>
              </div>
            </>
          ) : (
            <div className="rounded-lg border border-[color-mix(in_oklab,var(--color-hitl)_35%,transparent)] bg-[color-mix(in_oklab,var(--color-hitl)_8%,transparent)] p-3.5">
              <p className="text-[13px] font-medium text-hitl">
                This key may not resolve held actions.
              </p>
              <p className="mt-1.5 text-[12.5px] leading-relaxed text-ink-400">
                Its role is{" "}
                <span className="font-mono text-ink-200">{identity?.role ?? "agent"}</span>
                , and resolving needs{" "}
                <span className="font-mono text-ink-200">reviewer</span>. Human review only
                means anything if the party being reviewed cannot resolve it — an agent
                that can approve the action its own policy held has not been governed. The
                server refuses this with a 403; the button is hidden so you find out now
                rather than after clicking.
              </p>
            </div>
          )}
          <ErrorNote error={error} className="mt-3" />
        </div>
      )}
    </GlowCard>
  );
}

type Filter = "pending" | "resolved" | "all";

export function ReviewQueuePage() {
  const { session, status, can } = useSession();
  const connected = status === "connected";
  const [filter, setFilter] = useState<Filter>("pending");

  const queue = useAsync(
    () => (connected ? api.decisions(session, 100) : Promise.resolve(null)),
    [connected, session.baseUrl, session.apiKey],
  );

  const decisions = useMemo(() => queue.data?.decisions ?? [], [queue.data]);

  const counts = useMemo(
    () => ({
      pending: decisions.filter((d) => d.status === "pending").length,
      resolved: decisions.filter((d) => d.status !== "pending").length,
      all: decisions.length,
    }),
    [decisions],
  );

  const visible = decisions.filter((decision) =>
    filter === "all"
      ? true
      : filter === "pending"
        ? decision.status === "pending"
        : decision.status !== "pending",
  );

  const replace = (next: DecisionView) => {
    queue.setData({
      ...(queue.data ?? { count: 0, tenant_id: "" }),
      decisions: decisions.map((d) =>
        d.decision_id === next.decision_id ? next : d,
      ),
      count: decisions.length,
      tenant_id: queue.data?.tenant_id ?? "",
    });
  };

  if (!connected) {
    return (
      <EmptyState
        icon="⏸"
        title="Not connected"
        detail="The review queue is scoped to your tenant, which comes from the API key rather than a query parameter — so another tenant's held actions are invisible rather than merely forbidden."
        action={<Button onClick={() => (window.location.hash = "/connect")}>Connect</Button>}
      />
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-[26px] font-semibold tracking-tight text-ink-100">
            Review Queue
          </h1>
          <p className="mt-2 max-w-3xl text-[14px] leading-relaxed text-ink-400">
            Actions paused <em>before</em> execution, waiting on a person. Approving here
            releases the agent; denying returns a structured refusal it can explain. Every
            resolution is appended to the audit chain with the reviewer and their reason —
            an approval that leaves no trace is indistinguishable from no policy at all.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="outline" onClick={queue.reload}>
            Refresh
          </Button>
        </div>
      </div>

      {!can("resolve_decisions") && (
        <Card className="border-[color-mix(in_oklab,var(--color-hitl)_35%,transparent)] p-4">
          <p className="text-[13px] leading-relaxed text-ink-300">
            <span className="font-medium text-hitl">Read-only.</span> This key can
            see the queue but not resolve it. Use a key with the{" "}
            <span className="font-mono text-ink-100">reviewer</span> role to approve or
            deny — reading that your action is pending is useful and harmless; acting on it
            is the part that must be refused.
          </p>
        </Card>
      )}

      <Tabs
        value={filter}
        onChange={setFilter}
        tabs={[
          { id: "pending", label: "Awaiting review", count: counts.pending },
          { id: "resolved", label: "Settled", count: counts.resolved },
          { id: "all", label: "All", count: counts.all },
        ]}
      />

      <ErrorNote error={queue.error} />

      {queue.loading ? (
        <div className="space-y-3">
          <Skeleton className="h-40 w-full" />
          <Skeleton className="h-40 w-full" />
        </div>
      ) : visible.length === 0 ? (
        <EmptyState
          icon="⏸"
          title={filter === "pending" ? "Nothing is waiting" : "Nothing here yet"}
          detail={
            filter === "pending"
              ? "No action is currently held. Trigger one from the Agent Console — ask the agent to email an external address — or from Decision Theatre."
              : "Once actions are approved, denied, or time out, they appear here with the reviewer and reason recorded."
          }
          action={
            <Button onClick={() => (window.location.hash = "/agent")}>
              Make the agent trigger one
            </Button>
          }
        />
      ) : (
        <div className="space-y-3">
          <AnimatePresence initial={false}>
            {visible.map((decision) => (
              <motion.div
                key={decision.decision_id}
                layout
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, scale: 0.98 }}
                transition={{ duration: 0.25 }}
              >
                <DecisionCard
                  decision={decision}
                  canResolve={can("resolve_decisions")}
                  onResolved={replace}
                />
              </motion.div>
            ))}
          </AnimatePresence>
        </div>
      )}

      <Card className="p-5">
        <SectionTitle title="How a held decision ends" />
        <div className="grid gap-4 sm:grid-cols-3">
          <div className="rounded-lg border border-[color-mix(in_oklab,var(--color-allow)_35%,transparent)] bg-ink-900/40 p-4">
            <div className="font-mono text-[12px] font-semibold text-allow">
              APPROVED
            </div>
            <p className="mt-1.5 text-[12.5px] leading-relaxed text-ink-400">
              The waiting agent's next poll sees{" "}
              <span className="font-mono">allows_execution: true</span> and dispatches.
            </p>
          </div>
          <div className="rounded-lg border border-[color-mix(in_oklab,var(--color-block)_35%,transparent)] bg-ink-900/40 p-4">
            <div className="font-mono text-[12px] font-semibold text-block">
              DENIED
            </div>
            <p className="mt-1.5 text-[12.5px] leading-relaxed text-ink-400">
              The tool never runs. The agent receives the reason and reports it.
            </p>
          </div>
          <div className="rounded-lg border border-ink-700 bg-ink-900/40 p-4">
            <div className="font-mono text-[12px] font-semibold text-ink-300">EXPIRED</div>
            <p className="mt-1.5 text-[12.5px] leading-relaxed text-ink-400">
              Nobody answered in time, so <span className="font-mono">on_timeout</span>{" "}
              applies — <strong className="text-ink-200">deny</strong> by default. Silence
              must not become consent.
            </p>
          </div>
        </div>
        <p className="mt-4 text-[12.5px] leading-relaxed text-ink-500">
          Resolution is a DynamoDB conditional write, so two reviewers clicking at once
          produce exactly one winner and the loser gets a clean 409 rather than a silently
          overwritten decision.
        </p>
      </Card>
    </div>
  );
}
