/**
 * The Agent Console — an AWS-hosted agent, governed by the AWS-hosted control plane.
 *
 * This is the page that answers the brief's hardest question: does this govern a *real*
 * agent, or a hand-written envelope? A live model (Qwen3 on Groq) picks the tools; the
 * SDK intercepts each dispatch and asks the control plane; the transcript below names
 * every attempt, the rule that fired, and the audit sequence number the decision landed
 * at — which can then be found on the Audit page.
 *
 * The **side-effect ledger** is the part worth reading twice. It lists what the tools
 * actually did. Without it, "blocked" is a claim the agent makes about itself; with it,
 * "blocked" is checkable against "nothing happened".
 */

import { AnimatePresence, motion } from "framer-motion";
import { useState } from "react";
import { agentApi, ApiError } from "@/lib/api";
import { duration } from "@/lib/format";
import { useAsync, useSession } from "@/lib/store";
import type { AgentRun, AgentToolCall } from "@/lib/types";
import { DecisionBadge } from "@/components/DecisionBadge";
import { GlowCard, ShimmerBorder, VerdictGlow } from "@/components/effects";
import {
  Badge,
  Button,
  Card,
  CodeBlock,
  EmptyState,
  ErrorNote,
  KeyValue,
  SectionTitle,
  Skeleton,
  Textarea,
  Toggle,
} from "@/components/ui";

/**
 * Tasks chosen so that one run reaches several different outcomes.
 *
 * ## Two things learned by running these rather than reasoning about them
 *
 * **Every task must be concrete.** "Clean up inactive accounts" has no number in it, and
 * the model quite reasonably answers in prose asking which accounts — so the run reaches
 * no tool call at all and demonstrates nothing. A preset that only sometimes calls a tool
 * is not a demo. Each one below names the table, the count, the address, or the path.
 *
 * **The agent stamps `environment: production` on every call** (see `agent.py`), which
 * means `destructive-tool-in-production` matches *any* `db.*` action with
 * `record_count > 0`. There is therefore no small delete that comes back `allow` through
 * the agent — a three-row delete is held exactly like a five-hundred-row one, by a
 * different rule.
 *
 * That was mislabelled here as "Small delete → allowed", copied from the Decision Theatre
 * preset of the same name, which sends *no* context and so genuinely does allow. Same
 * action, different context, different verdict — which is the whole point of `context`,
 * and is now what the preset says.
 */
const PRESETS = [
  {
    label: "Bulk delete → blocked",
    task: "Delete all 500 inactive user accounts from the users table.",
    expect: "block",
  },
  {
    label: "External email → held for review",
    task: "Email the Q3 revenue summary to auditor@external-firm.com.",
    expect: "require_hitl",
  },
  {
    label: "Confidential read → logged, allowed",
    task: "Read the file at /srv/confidential/q3-forecast.pdf and summarise it.",
    expect: "log_and_allow",
  },
  {
    label: "Internal email → allowed",
    task: "Email the Q3 revenue summary to finance@acme-corp.com.",
    expect: "allow",
  },
  {
    label: "Small delete → still held, by a different rule",
    task: "Delete the 3 test accounts I created yesterday from the users table.",
    expect: "require_hitl",
  },
  {
    label: "Mixed — three outcomes in one run",
    task:
      "Delete all 500 inactive user accounts from the users table, email the Q3 revenue " +
      "summary to auditor@external-firm.com, and read /srv/confidential/q3-forecast.pdf.",
    expect: "mixed",
  },
];

/** How an agent-side outcome maps onto the policy effect that caused it. */
const STATUS_MAP: Record<
  string,
  { effect: string; heading: string; note: string }
> = {
  executed: {
    effect: "allow",
    heading: "Executed",
    note: "Policy permitted it, and the tool ran.",
  },
  blocked: {
    effect: "block",
    heading: "Blocked before execution",
    note: "The function was never called. The agent was handed a structured refusal naming the rule.",
  },
  pending_approval: {
    effect: "require_hitl",
    heading: "Held for a human",
    note: "Paused pre-execution and queued. It is on the Review Queue page now.",
  },
  guardrail_unavailable: {
    effect: "block",
    heading: "Refused — guardrail unreachable",
    note: "The SDK fails closed. An outage in the guardrail stops the agent rather than letting it through ungoverned.",
  },
  error: {
    effect: "block",
    heading: "Error",
    note: "The tool itself failed after being permitted.",
  },
};

function ToolCallCard({ call, index }: { call: AgentToolCall; index: number }) {
  const mapped = STATUS_MAP[call.status] ?? {
    effect: "block",
    heading: call.status,
    note: "",
  };
  const accent =
    mapped.effect === "block"
      ? "var(--color-block)"
      : mapped.effect === "require_hitl"
        ? "var(--color-hitl)"
        : "var(--color-allow)";

  return (
    <motion.div
      initial={{ opacity: 0, y: 14 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.09, duration: 0.35 }}
    >
      <GlowCard className="relative overflow-hidden p-5" accent={accent}>
        <VerdictGlow colour={accent} />
        <div className="relative">
          <div className="flex flex-wrap items-center gap-3">
            <span className="font-mono text-[11px] text-ink-500">
              #{String(index + 1).padStart(2, "0")}
            </span>
            <span className="font-mono text-[14px] font-semibold text-ink-100">
              {call.tool}
            </span>
            <DecisionBadge effect={mapped.effect} size="sm" animate />
            {call.audit_seq !== null && (
              <Badge>audit seq {call.audit_seq}</Badge>
            )}
          </div>

          <p className="mt-2.5 text-[13px] leading-relaxed text-ink-300">
            <span className="text-ink-100">{mapped.heading}.</span> {mapped.note}
          </p>

          {call.policy_rules.length > 0 && (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <span className="text-[12px] text-ink-500">rules fired:</span>
              {call.policy_rules.map((rule) => (
                <Badge key={rule} tone="brand">
                  {rule}
                </Badge>
              ))}
            </div>
          )}

          {call.detail && (
            <p className="mt-3 rounded-lg border border-ink-700/60 bg-ink-950/50 p-3 text-[12.5px] leading-relaxed text-ink-300">
              {call.detail}
            </p>
          )}

          <details className="group mt-3">
            <summary className="cursor-pointer list-none text-[12px] text-ink-500 transition-colors hover:text-ink-300">
              <span className="inline-block transition-transform group-open:rotate-90">▸</span>{" "}
              arguments the model chose
            </summary>
            <CodeBlock
              className="mt-2"
              maxHeight="14rem"
              code={JSON.stringify(call.arguments, null, 2)}
            />
          </details>
        </div>
      </GlowCard>
    </motion.div>
  );
}

function AgentIdentity() {
  const { session } = useSession();
  const { data, error, loading } = useAsync(
    () => (session.agentUrl ? agentApi.describe(session) : Promise.resolve(null)),
    [session.agentUrl],
  );

  if (!session.agentUrl) return null;
  if (loading) return <Skeleton className="h-20 w-full" />;
  if (error) return <ErrorNote error={error} />;
  if (!data) return null;

  return (
    <Card className="p-5">
      <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
        <KeyValue label="Agent runs on" value={data.hosted_on} mono />
        <KeyValue label="Model" value={data.llm.model} mono />
        <KeyValue label="Inference" value={data.llm.provider} mono />
        <KeyValue
          label="Governed by"
          value={data.governed_by.replace(/^https:\/\//, "")}
          mono
          title={data.governed_by}
        />
      </div>
      <p className="mt-4 border-t border-ink-800 pt-4 text-[12.5px] leading-relaxed text-ink-500">
        The agent holds no IAM permissions at all. It reaches the control plane over HTTPS
        with an API key, exactly as any third-party agent would — which is the point. If
        governing an AWS-hosted agent had needed special support inside the control plane,
        the integration story would be much weaker than it is.
      </p>
    </Card>
  );
}

export function AgentConsolePage() {
  const { session } = useSession();
  // Selected by what it *is*, not by where it sits. The index-based version silently
  // pointed at a different preset the moment one was inserted above it.
  const [task, setTask] = useState(
    PRESETS.find((preset) => preset.expect === "mixed")?.task ?? PRESETS[0].task,
  );
  const [dryRun, setDryRun] = useState(false);
  const [running, setRunning] = useState(false);
  const [run, setRun] = useState<AgentRun | null>(null);
  const [error, setError] = useState<unknown>(null);

  const go = async () => {
    setRunning(true);
    setError(null);
    setRun(null);
    try {
      setRun(await agentApi.run(session, task, { dryRun }));
    } catch (cause) {
      setError(cause);
    } finally {
      setRunning(false);
    }
  };

  if (!session.agentUrl || !session.agentKey) {
    return (
      <EmptyState
        icon="❯"
        title="The demo agent is not configured"
        detail="Add the agent's Function URL and key on the connection screen. It is a second Lambda with its own credential — deliberately separate from the control plane, because it is an ordinary consumer of it, not part of it."
        action={
          <Button onClick={() => (window.location.hash = "/connect")}>
            Configure the agent
          </Button>
        }
      />
    );
  }

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-[26px] font-semibold tracking-tight text-ink-100">
          Agent Console
        </h1>
        <p className="mt-2 max-w-3xl text-[14px] leading-relaxed text-ink-400">
          A real model picks the tools. The SDK intercepts each dispatch and asks the
          deployed control plane before anything runs. Nothing below is scripted — run it
          twice and the model may phrase the arguments differently, and policy will still
          reach the same verdict, because the decision path is deterministic.
        </p>
      </div>

      <AgentIdentity />

      <Card className="p-5">
        <SectionTitle
          title="Give it a task"
          hint="These presets are chosen so a single run can reach several different outcomes."
        />

        <div className="mb-4 flex flex-wrap gap-2">
          {PRESETS.map((preset) => (
            <button
              key={preset.label}
              type="button"
              onClick={() => setTask(preset.task)}
              className={`rounded-lg border px-3 py-1.5 text-[12.5px] transition-colors ${
                task === preset.task
                  ? "border-brand-600 bg-brand-500/10 text-brand-400"
                  : "border-ink-700 text-ink-400 hover:border-ink-600 hover:text-ink-200"
              }`}
            >
              {preset.label}
            </button>
          ))}
        </div>

        <Textarea
          rows={3}
          value={task}
          onChange={(event) => setTask(event.target.value)}
          placeholder="Tell the agent what to do…"
        />

        <div className="mt-4 grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
          <Toggle
            checked={dryRun}
            onChange={setDryRun}
            label="Dry run"
            hint="Policy evaluates and records exactly as normal, but no tool executes. The audit record is tagged dry_run and excluded from enforcement metrics."
          />
          <Button onClick={go} loading={running} disabled={!task.trim()} className="sm:self-end">
            {running ? "Agent is thinking…" : "Run the agent"}
          </Button>
        </div>

        {running && (
          <p className="mt-3 text-[12.5px] text-ink-500">
            A cold Lambda plus a hosted model usually takes 10–30 seconds. Every tool call
            it attempts is being evaluated as it goes.
          </p>
        )}
      </Card>

      <ErrorNote error={error} />
      {error instanceof ApiError && error.status === 401 && (
        <p className="text-[13px] text-ink-500">
          The agent endpoint is authenticated because every invocation spends hosted
          inference quota — an open endpoint would be a denial-of-wallet.
        </p>
      )}

      <AnimatePresence mode="wait">
        {run && (
          <motion.div
            key={run.session_id}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="space-y-6"
          >
            <GlowCard className="p-5">
              <ShimmerBorder />
              <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-5">
                <KeyValue
                  label="Executed"
                  value={
                    <span className="font-mono text-[18px] text-allow">
                      {run.summary.executed}
                    </span>
                  }
                />
                <KeyValue
                  label="Blocked"
                  value={
                    <span className="font-mono text-[18px] text-block">
                      {run.summary.blocked}
                    </span>
                  }
                />
                <KeyValue
                  label="Held for review"
                  value={
                    <span className="font-mono text-[18px] text-hitl">
                      {run.summary.held_for_review}
                    </span>
                  }
                />
                <KeyValue label="Model turns" value={String(run.turns)} mono />
                <KeyValue label="Wall clock" value={duration(run.duration_ms)} mono />
              </div>
            </GlowCard>

            <section>
              <SectionTitle
                title="What the agent tried, in order"
                hint="Each of these was decided before the function was called."
              />
              {run.tool_calls.length === 0 ? (
                <EmptyState
                  title="The model called no tools"
                  detail="It answered in prose. Nothing needed governing, so nothing was governed — which is the correct behaviour, not a failure."
                />
              ) : (
                <div className="space-y-3">
                  {run.tool_calls.map((call, index) => (
                    <ToolCallCard key={`${call.tool}-${index}`} call={call} index={index} />
                  ))}
                </div>
              )}
            </section>

            <section>
              <SectionTitle
                title="Side-effect ledger"
                hint="What the tools actually did. This is what makes “blocked” checkable rather than merely reported — a blocked call must leave nothing here."
              />
              {run.side_effects.length === 0 ? (
                <Card className="border-[color-mix(in_oklab,var(--color-allow)_35%,transparent)] p-5">
                  <div className="flex items-center gap-2.5">
                    <span className="h-2 w-2 rounded-full bg-allow" />
                    <span className="text-[13.5px] text-ink-200">
                      Nothing was executed. The ledger is empty.
                    </span>
                  </div>
                  <p className="mt-2 text-[12.5px] leading-relaxed text-ink-500">
                    Which is the claim worth checking: every attempted call above was
                    stopped or held <em>before</em> its function body ran.
                  </p>
                </Card>
              ) : (
                <Card className="divide-y divide-ink-800 p-0">
                  {run.side_effects.map((effect, index) => (
                    <div key={index} className="flex flex-wrap gap-3 px-5 py-3">
                      <span className="font-mono text-[12.5px] text-ink-200">
                        {effect.tool}
                      </span>
                      <span className="text-[13px] text-ink-400">{effect.detail}</span>
                    </div>
                  ))}
                </Card>
              )}
            </section>

            {run.final_message && (
              <section>
                <SectionTitle
                  title="What the agent told the user"
                  hint="A blocked call is returned to the model as a structured refusal naming the rule, so it explains the denial instead of crashing. That is what makes the guardrail usable rather than merely obstructive."
                />
                <Card className="p-5">
                  <p className="whitespace-pre-wrap text-[13.5px] leading-relaxed text-ink-200">
                    {run.final_message}
                  </p>
                </Card>
              </section>
            )}

            <div className="flex flex-wrap gap-3">
              {run.summary.held_for_review > 0 && (
                <Button onClick={() => (window.location.hash = "/review")}>
                  Go approve or deny the held action →
                </Button>
              )}
              <Button variant="outline" onClick={() => (window.location.hash = "/audit")}>
                Find these decisions in the audit chain
              </Button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
