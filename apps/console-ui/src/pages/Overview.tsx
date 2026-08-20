/**
 * Overview — the operational dashboard.
 *
 * This page used to open with a thesis: an animated headline, an argument about how text
 * guardrails differ from action guardrails, a pipeline walkthrough, and a panel of project
 * statistics. All of it was true and none of it belonged in the product. A home screen in
 * an operations console answers "what is the state of the system right now", not "why was
 * this system built".
 *
 * So it answers that: what policy is in force, whether the chain still verifies, whether
 * anything is waiting on a human, and what has been decided recently. Everything here is
 * read live from the deployed control plane and scoped to the caller's tenant.
 *
 * The explanatory material was not deleted — it moved to a reference document, where a
 * reader who wants the argument can find it and an operator who does not is unaffected.
 *
 * Anything genuinely wrong (a broken chain, a failed readiness probe) is stated *above*
 * the tiles rather than among them. A number in a grid is scanned; a banner is read.
 */

import { api } from "@/lib/api";
import { effectStyle, relativeTime } from "@/lib/format";
import { useAsync, useSession } from "@/lib/store";
import type { Effect } from "@/lib/types";
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
} from "@/components/ui";

const OUTCOMES: Effect[] = ["block", "require_hitl", "log_and_allow", "allow"];

export function OverviewPage() {
  const { session, status, identity } = useSession();
  const connected = status === "connected";

  const deps = [connected, session.baseUrl, session.apiKey];

  const verify = useAsync(() => (connected ? api.verify(session) : Promise.resolve(null)), deps);
  const audit = useAsync(() => (connected ? api.audit(session, 100) : Promise.resolve(null)), deps);
  const policy = useAsync(
    () => (connected ? api.activePolicy(session) : Promise.resolve(null)),
    deps,
  );
  const queue = useAsync(
    () => (connected ? api.decisions(session, 50) : Promise.resolve(null)),
    deps,
  );
  const ready = useAsync(() => (connected ? api.readiness(session) : Promise.resolve(null)), deps);

  if (!connected) {
    return (
      <EmptyState
        icon="◆"
        title="Not connected"
        detail="Everything on this page is read live from the control plane, scoped to the tenant on your API key."
        action={<Button onClick={() => (window.location.hash = "/connect")}>Connect</Button>}
      />
    );
  }

  const entries = audit.data?.entries ?? [];
  const pending = (queue.data?.decisions ?? []).filter((d) => d.status === "pending");
  const counts = entries.reduce<Record<string, number>>(
    (acc, entry) => ({ ...acc, [entry.effect]: (acc[entry.effect] ?? 0) + 1 }),
    {},
  );

  const chainBroken = verify.data?.chain_valid === false;
  const notReady = ready.data?.ready === false;

  const refreshAll = () => {
    verify.reload();
    audit.reload();
    policy.reload();
    queue.reload();
    ready.reload();
  };

  return (
    <div className="space-y-7">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <h1 className="text-[26px] font-semibold tracking-tight text-ink-100">Overview</h1>
        <div className="flex items-center gap-3">
          {identity && (
            <span className="font-mono text-[12px] text-ink-500">
              tenant {identity.tenant_id}
            </span>
          )}
          <Button size="sm" variant="outline" onClick={refreshAll}>
            Refresh
          </Button>
        </div>
      </div>

      {chainBroken && (
        <Card className="border-[color-mix(in_oklab,var(--color-block)_45%,transparent)] p-4">
          <p className="text-[13.5px] text-block">
            <strong>Audit chain broken</strong> at sequence{" "}
            {verify.data?.broken_at_seq ?? "unknown"} — {verify.data?.reason}
          </p>
          <Button
            size="sm"
            variant="outline"
            className="mt-3"
            onClick={() => (window.location.hash = "/audit")}
          >
            Investigate
          </Button>
        </Card>
      )}
      {notReady && (
        <Card className="border-[color-mix(in_oklab,var(--color-block)_45%,transparent)] p-4">
          <p className="text-[13.5px] text-block">
            <strong>Not ready</strong> — a dependency needed to serve decisions is
            unavailable. Governed agents are being refused.
          </p>
        </Card>
      )}

      {/* ------------------------------------------------------------- status */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Card className="p-5">
          <div className="text-[11px] uppercase tracking-wider text-ink-500">
            Awaiting review
          </div>
          {queue.loading ? (
            <Skeleton className="mt-2 h-8 w-16" />
          ) : (
            <>
              <div
                className={`mt-1 font-mono text-[26px] font-semibold ${
                  pending.length > 0 ? "text-hitl" : "text-ink-100"
                }`}
              >
                {pending.length}
              </div>
              {pending.length > 0 && (
                <button
                  type="button"
                  className="mt-1 text-[12.5px] text-brand-400 hover:underline"
                  onClick={() => (window.location.hash = "/review")}
                >
                  Open the queue →
                </button>
              )}
            </>
          )}
        </Card>

        <Card className="p-5">
          <div className="text-[11px] uppercase tracking-wider text-ink-500">
            Policy in force
          </div>
          {policy.loading ? (
            <Skeleton className="mt-2 h-8 w-20" />
          ) : policy.data ? (
            <>
              <div className="mt-1 font-mono text-[26px] font-semibold text-ink-100">
                v{policy.data.version}
              </div>
              <div className="mt-1 flex flex-wrap gap-1.5">
                <Badge>{policy.data.source}</Badge>
                {policy.data.mode === "shadow" && <Badge tone="warn">shadow</Badge>}
              </div>
            </>
          ) : (
            <div className="mt-2 text-[13px] text-ink-500">unavailable</div>
          )}
        </Card>

        <Card className="p-5">
          <div className="text-[11px] uppercase tracking-wider text-ink-500">Audit chain</div>
          {verify.loading ? (
            <Skeleton className="mt-2 h-8 w-24" />
          ) : verify.data ? (
            <>
              <div
                className={`mt-1 text-[19px] font-semibold ${
                  verify.data.chain_valid ? "text-allow" : "text-block"
                }`}
              >
                {verify.data.chain_valid ? "verified" : "BROKEN"}
              </div>
              <div className="mt-1 font-mono text-[12px] text-ink-500">
                {verify.data.records_checked} records
              </div>
            </>
          ) : (
            <div className="mt-2 text-[13px] text-ink-500">unavailable</div>
          )}
        </Card>

        <Card className="p-5">
          <div className="text-[11px] uppercase tracking-wider text-ink-500">Readiness</div>
          {ready.loading ? (
            <Skeleton className="mt-2 h-8 w-20" />
          ) : ready.data ? (
            <>
              <div
                className={`mt-1 text-[19px] font-semibold ${
                  ready.data.ready ? "text-allow" : "text-block"
                }`}
              >
                {ready.data.ready ? "ready" : "not ready"}
              </div>
              <div className="mt-1 font-mono text-[12px] text-ink-500">
                {ready.data.dependencies.length} dependencies
              </div>
            </>
          ) : (
            <div className="mt-2 text-[13px] text-ink-500">unavailable</div>
          )}
        </Card>
      </div>

      {/* ------------------------------------------------ decisions breakdown */}
      <GlowCard className="p-5">
        <SectionTitle
          title="Recent decisions"
          hint={`Last ${entries.length} records in this tenant's chain.`}
        />
        <ErrorNote error={audit.error} />
        {audit.loading ? (
          <Skeleton className="h-16 w-full" />
        ) : entries.length === 0 ? (
          <p className="text-[13px] text-ink-500">No decisions recorded yet.</p>
        ) : (
          <div className="grid gap-4 sm:grid-cols-4">
            {OUTCOMES.map((effect) => (
              <div key={effect}>
                <div
                  className={`font-mono text-[22px] font-semibold ${effectStyle(effect).text}`}
                >
                  {counts[effect] ?? 0}
                </div>
                <div className="mt-0.5 font-mono text-[11.5px] text-ink-500">{effect}</div>
              </div>
            ))}
          </div>
        )}
      </GlowCard>

      {/* ----------------------------------------------------------- activity */}
      <Card className="p-5">
        <SectionTitle
          title="Latest activity"
          action={
            <Button
              size="sm"
              variant="ghost"
              onClick={() => (window.location.hash = "/audit")}
            >
              Full audit log →
            </Button>
          }
        />
        {audit.loading ? (
          <Skeleton className="h-32 w-full" />
        ) : entries.length === 0 ? (
          <p className="text-[13px] text-ink-500">Nothing yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[560px] text-left">
              <thead>
                <tr className="border-b border-ink-800">
                  {["Seq", "Tool", "Decision", "Agent", "When"].map((header) => (
                    <th
                      key={header}
                      className="pb-2 pr-4 text-[11px] uppercase tracking-wider text-ink-500"
                    >
                      {header}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {entries.slice(0, 10).map((entry) => (
                  <tr key={entry.seq} className="border-b border-ink-800/60 last:border-0">
                    <td className="py-2.5 pr-4 font-mono text-[12px] text-ink-500">
                      {entry.seq}
                    </td>
                    <td className="py-2.5 pr-4 font-mono text-[12.5px] text-ink-100">
                      {entry.tool}
                    </td>
                    <td className="py-2.5 pr-4">
                      <DecisionBadge effect={entry.effect} size="sm" />
                    </td>
                    <td className="py-2.5 pr-4 font-mono text-[12px] text-ink-400">
                      {entry.agent_id}
                    </td>
                    <td className="py-2.5 font-mono text-[11.5px] text-ink-500">
                      {relativeTime(entry.timestamp)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* -------------------------------------------------------- deployment */}
      <Card className="p-5">
        <SectionTitle title="This deployment" />
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <KeyValue label="Stage" value={identity?.stage ?? "—"} mono />
          <KeyValue label="Tenant" value={identity?.tenant_id ?? "—"} mono />
          <KeyValue label="Your key" value={identity?.key_id ?? "—"} mono />
          <KeyValue label="Your role" value={identity?.role ?? "—"} mono />
        </div>
      </Card>
    </div>
  );
}
