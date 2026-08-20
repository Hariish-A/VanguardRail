/**
 * The landing page: what this system is, and what it has actually been shown to do.
 *
 * Everything numeric on this page is either fetched live from the deployed control plane
 * or is a figure this repository can reproduce on demand. Nothing here is aspirational —
 * a governance product that overstates itself on its own front page has already lost the
 * argument it exists to make.
 */

import { motion } from "framer-motion";
import { api } from "@/lib/api";
import { EFFECT_MEANING, effectStyle, relativeTime } from "@/lib/format";
import { useAsync, useSession } from "@/lib/store";
import type { Effect } from "@/lib/types";
import { DecisionBadge } from "@/components/DecisionBadge";
import {
  Aurora,
  GlowCard,
  GridField,
  Meteors,
  NumberTicker,
  ShimmerBorder,
  Spotlight,
  TextGenerate,
} from "@/components/effects";
import { Badge, Button, Card, KeyValue, SectionTitle, Skeleton } from "@/components/ui";

const OUTCOMES: Effect[] = ["allow", "log_and_allow", "require_hitl", "block"];

const PIPELINE = [
  {
    step: "Model emits a tool call",
    detail:
      "Qwen3 on Groq (hosted) or Ollama (local) decides to call db.delete_records with 500 rows.",
    tone: "text-ink-300",
  },
  {
    step: "SDK intercepts, pre-execution",
    detail:
      "@governed_tool wraps the dispatch. Nothing has run yet — this is the whole point.",
    tone: "text-brand-400",
  },
  {
    step: "Facts are derived",
    detail:
      "record_count, recipient_domains, path — normalised, so one rule covers every way an argument can be phrased.",
    tone: "text-ink-300",
  },
  {
    step: "Every rule is evaluated",
    detail:
      "Not first-match. All matches are collected and the most restrictive wins, so reordering the file cannot silently weaken policy.",
    tone: "text-ink-300",
  },
  {
    step: "The decision is chained",
    detail:
      "sha256(prev_hash ‖ canonical_json(record)) into DynamoDB, before the response is returned.",
    tone: "text-brand-400",
  },
  {
    step: "The agent is told, and can explain",
    detail:
      "A structured refusal naming the rule — so the agent reasons about the denial instead of crashing.",
    tone: "text-ink-300",
  },
];

const PROOF = [
  { value: 425, suffix: "", label: "tests", note: "unit, property, chaos, infra" },
  { value: 20, suffix: "/20", label: "conformance", note: "against live AWS" },
  { value: 15, suffix: "/25", label: "DynamoDB WCU", note: "free-tier provisioned" },
  { value: 10, suffix: "/10", label: "CloudWatch metrics", note: "budget exactly full" },
  { value: 0, suffix: ".00", label: "USD spent", note: "always-free tier only" },
];

function LiveStrip() {
  const { session, status } = useSession();
  const connected = status === "connected";

  const verify = useAsync(
    () => (connected ? api.verify(session) : Promise.resolve(null)),
    [connected, session.baseUrl, session.apiKey],
  );
  const audit = useAsync(
    () => (connected ? api.audit(session, 50) : Promise.resolve(null)),
    [connected, session.baseUrl, session.apiKey],
  );
  const policies = useAsync(
    () => (connected ? api.policies(session) : Promise.resolve(null)),
    [connected, session.baseUrl, session.apiKey],
  );

  if (!connected) {
    return (
      <Card className="flex flex-wrap items-center gap-x-6 gap-y-3 p-4">
        <span className="text-[13px] text-ink-400">
          Connect a key to show live figures from the deployed control plane.
        </span>
        <Button size="sm" variant="outline" onClick={() => (window.location.hash = "/connect")}>
          Connect
        </Button>
      </Card>
    );
  }

  const loading = verify.loading || audit.loading || policies.loading;
  const counts = (audit.data?.entries ?? []).reduce<Record<string, number>>(
    (acc, entry) => ({ ...acc, [entry.effect]: (acc[entry.effect] ?? 0) + 1 }),
    {},
  );

  return (
    <GlowCard className="p-5">
      <ShimmerBorder />
      <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <div className="text-[11px] uppercase tracking-wider text-ink-500">Audit chain</div>
          {loading ? (
            <Skeleton className="mt-2 h-6 w-32" />
          ) : verify.data ? (
            <div className="mt-1.5 flex items-center gap-2">
              <span
                className={
                  verify.data.chain_valid
                    ? "text-[15px] font-semibold text-allow"
                    : "text-[15px] font-semibold text-block"
                }
              >
                {verify.data.chain_valid ? "intact" : "BROKEN"}
              </span>
              <span className="font-mono text-[12px] text-ink-500">
                {verify.data.records_checked} records
              </span>
            </div>
          ) : (
            <div className="mt-1.5 text-[13px] text-ink-500">unavailable</div>
          )}
        </div>

        <div>
          <div className="text-[11px] uppercase tracking-wider text-ink-500">
            Recent decisions
          </div>
          {loading ? (
            <Skeleton className="mt-2 h-6 w-40" />
          ) : (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {OUTCOMES.filter((effect) => counts[effect]).map((effect) => (
                <span
                  key={effect}
                  className={`font-mono text-[12px] ${effectStyle(effect).text}`}
                >
                  {counts[effect]} {effect}
                </span>
              ))}
              {Object.keys(counts).length === 0 && (
                <span className="text-[13px] text-ink-500">none yet</span>
              )}
            </div>
          )}
        </div>

        <div>
          <div className="text-[11px] uppercase tracking-wider text-ink-500">
            Policy in force
          </div>
          {loading ? (
            <Skeleton className="mt-2 h-6 w-28" />
          ) : policies.data ? (
            <div className="mt-1.5 font-mono text-[13px] text-ink-100">
              v{policies.data.active_version ?? 1}{" "}
              <span className="text-ink-500">({policies.data.active_source})</span>
            </div>
          ) : (
            <div className="mt-1.5 text-[13px] text-ink-500">unavailable</div>
          )}
        </div>

        <div>
          <div className="text-[11px] uppercase tracking-wider text-ink-500">Last decision</div>
          {loading ? (
            <Skeleton className="mt-2 h-6 w-36" />
          ) : audit.data?.entries[0] ? (
            <div className="mt-1.5 flex items-center gap-2">
              <DecisionBadge effect={audit.data.entries[0].effect} size="sm" />
              <span className="font-mono text-[12px] text-ink-500">
                {relativeTime(audit.data.entries[0].timestamp)}
              </span>
            </div>
          ) : (
            <div className="mt-1.5 text-[13px] text-ink-500">no records</div>
          )}
        </div>
      </div>
    </GlowCard>
  );
}

export function OverviewPage() {
  return (
    <div className="space-y-16">
      <Aurora />

      {/* ---------------------------------------------------------------- Hero */}
      <section className="relative -mt-4 overflow-hidden rounded-3xl border border-ink-800 bg-ink-900/40 px-6 py-16 sm:px-12 sm:py-20">
        <GridField />
        <Spotlight />
        <Meteors count={12} />

        <div className="relative max-w-4xl">
          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5 }}
            className="mb-6 flex flex-wrap items-center gap-2"
          >
            <Badge tone="brand">Problem statement PS-3.1</Badge>
            <Badge>deployed on AWS · us-east-1</Badge>
            <Badge tone="good">$0.00 spend</Badge>
          </motion.div>

          <h1 className="text-[34px] font-semibold leading-[1.1] tracking-tight sm:text-[52px]">
            <TextGenerate text="Guardrails for what an agent" className="text-ink-100" />{" "}
            <span className="text-gradient animate-shimmer">does</span>
            <span className="text-ink-500">,</span>
            <br />
            <TextGenerate
              text="not what the model says."
              className="text-ink-400"
              delay={0.55}
            />
          </h1>

          <motion.p
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 1.1, duration: 0.6 }}
            className="mt-7 max-w-2xl text-[15px] leading-relaxed text-ink-300"
          >
            Every commercial guardrail filters model <em>text</em>. A perfectly clean
            response can still tell a tool to delete 10,000 rows, email a competitor, or
            read a confidential path — and today's guardrails pass it straight through.
            This is the missing layer: every tool call is evaluated against declarative
            policy <strong className="text-ink-100">before it executes</strong>, and every
            decision is written into a hash-chained audit record.
          </motion.p>

          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 1.3, duration: 0.5 }}
            className="mt-9 flex flex-wrap gap-3"
          >
            <Button onClick={() => (window.location.hash = "/agent")}>
              Watch an agent be governed →
            </Button>
            <Button variant="outline" onClick={() => (window.location.hash = "/theatre")}>
              Send a tool call yourself
            </Button>
          </motion.div>
        </div>
      </section>

      {/* -------------------------------------------------------------- Live */}
      <section>
        <SectionTitle
          title="Live, from the deployed control plane"
          hint="Read from AWS right now, not from a fixture."
        />
        <LiveStrip />
      </section>

      {/* ---------------------------------------------------------- The gap */}
      <section>
        <SectionTitle
          title="The gap this closes"
          hint="Both of these are the same conversation. Only one of them is governed by an action layer."
        />
        <div className="grid gap-4 lg:grid-cols-2">
          <Card className="overflow-hidden p-0">
            <div className="border-b border-ink-800 bg-ink-900/60 px-5 py-3">
              <span className="font-mono text-[12px] uppercase tracking-wider text-ink-400">
                A text guardrail sees
              </span>
            </div>
            <div className="space-y-3 p-5">
              <p className="rounded-lg border border-ink-700/60 bg-ink-900/50 p-3 text-[13px] leading-relaxed text-ink-300">
                “I'll tidy up the inactive accounts for you now.”
              </p>
              <div className="flex items-center gap-2">
                <DecisionBadge effect="allow" size="sm" />
                <span className="text-[13px] text-ink-400">
                  Polite, non-toxic, no PII. Nothing to flag.
                </span>
              </div>
            </div>
          </Card>

          <Card className="overflow-hidden border-[color-mix(in_oklab,var(--color-block)_35%,transparent)] p-0">
            <div className="border-b border-ink-800 bg-ink-900/60 px-5 py-3">
              <span className="font-mono text-[12px] uppercase tracking-wider text-ink-400">
                An action guardrail sees
              </span>
            </div>
            <div className="space-y-3 p-5">
              <pre className="overflow-x-auto rounded-lg border border-ink-700/60 bg-ink-950/70 p-3 font-mono text-[12.5px] leading-relaxed text-ink-200">
                {`db.delete_records
  table:  users
  where:  last_login < '2024-01-01'
  → derived.record_count = 500`}
              </pre>
              <div className="flex items-center gap-2">
                <DecisionBadge effect="block" size="sm" />
                <span className="text-[13px] text-ink-400">
                  Rule <span className="font-mono text-ink-200">db-bulk-delete</span>. The
                  tool never ran.
                </span>
              </div>
            </div>
          </Card>
        </div>
      </section>

      {/* ------------------------------------------------------- Four effects */}
      <section>
        <SectionTitle
          title="Four outcomes, one ordering"
          hint="block > require_hitl > log_and_allow > allow. Every matching rule is evaluated and the most restrictive wins — so adding a rule can only ever tighten policy, never loosen it by accident."
        />
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {OUTCOMES.map((effect, index) => (
            <motion.div
              key={effect}
              initial={{ opacity: 0, y: 12 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: "-60px" }}
              transition={{ delay: index * 0.07, duration: 0.4 }}
            >
              <GlowCard
                className="h-full p-5"
                accent={`var(--color-${effect === "log_and_allow" ? "log" : effect === "require_hitl" ? "hitl" : effect})`}
              >
                <DecisionBadge effect={effect} size="sm" />
                <p className="mt-3 text-[13px] leading-relaxed text-ink-300">
                  {EFFECT_MEANING[effect]}
                </p>
              </GlowCard>
            </motion.div>
          ))}
        </div>
      </section>

      {/* ---------------------------------------------------------- Pipeline */}
      <section>
        <SectionTitle
          title="What happens between the model and the database"
          hint="Six steps, all of them before the tool runs. The decision path never calls an LLM — it is deterministic, so it is testable and it cannot be prompt-injected."
        />
        <Card className="p-6">
          <ol className="relative space-y-6 border-l border-ink-700 pl-8">
            {PIPELINE.map((stage, index) => (
              <motion.li
                key={stage.step}
                initial={{ opacity: 0, x: -8 }}
                whileInView={{ opacity: 1, x: 0 }}
                viewport={{ once: true, margin: "-40px" }}
                transition={{ delay: index * 0.06, duration: 0.35 }}
                className="relative"
              >
                <span className="absolute -left-[41px] flex h-6 w-6 items-center justify-center rounded-full border border-ink-600 bg-ink-900 font-mono text-[11px] text-ink-400">
                  {index + 1}
                </span>
                <div className={`text-[14px] font-medium ${stage.tone}`}>{stage.step}</div>
                <p className="mt-1 max-w-2xl text-[13px] leading-relaxed text-ink-400">
                  {stage.detail}
                </p>
              </motion.li>
            ))}
          </ol>
        </Card>
      </section>

      {/* ------------------------------------------------------------- Proof */}
      <section>
        <SectionTitle
          title="Verified, not asserted"
          hint="Each of these is reproducible from this repository. The load-test figure is deliberately the sustained number, not the burst one."
        />
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          {PROOF.map((stat) => (
            <Card key={stat.label} className="p-5">
              <div className="font-mono text-[26px] font-semibold text-ink-100">
                <NumberTicker value={stat.value} suffix={stat.suffix} />
              </div>
              <div className="mt-1 text-[13px] text-ink-300">{stat.label}</div>
              <div className="mt-0.5 text-[12px] text-ink-500">{stat.note}</div>
            </Card>
          ))}
        </div>
      </section>

      {/* --------------------------------------------------- The real defect */}
      <section>
        <GlowCard className="p-6" accent="var(--color-hitl)">
          <div className="flex flex-wrap items-center gap-3">
            <Badge tone="warn">found in our own system</Badge>
            <span className="text-[13px] text-ink-500">
              verified live, then closed, then regression-tested
            </span>
          </div>
          <h3 className="mt-4 max-w-3xl text-[19px] font-semibold leading-snug text-ink-100">
            For a while, the agent could approve the action its own policy had held.
          </h3>
          <div className="mt-4 grid gap-5 lg:grid-cols-2">
            <p className="text-[13.5px] leading-relaxed text-ink-300">
              <span className="font-mono text-ink-200">/v1/decisions/&#123;id&#125;/resolve</span>{" "}
              required only a <em>valid</em> API key — not a particular one. So{" "}
              <span className="font-mono text-ink-200">require_hitl</span>, whose entire
              purpose is “pause for a human”, in practice meant “pause for anyone holding a
              key” — and the agent being paused held one. Against the live deployment, the
              AWS-hosted agent had an external email held and then approved it with its own
              key. The audit chain recorded{" "}
              <span className="font-mono text-block">
                reviewer: the-agent-itself
              </span>
              .
            </p>
            <p className="text-[13.5px] leading-relaxed text-ink-400">
              The identical argument was already written down one section over, for policy
              administration — <em>“an agent whose key can rewrite the policy governing it
              is not governed”</em> — and had simply never been extended to approval.
              Reasoning about one privilege correctly does not generalise on its own. It is
              closed by the role model: resolving now needs{" "}
              <span className="font-mono text-ink-200">reviewer</span>, the same attack
              returns <span className="font-mono text-allow">403</span> with the
              decision still pending, and a regression test would fail loudly if that ever
              regressed.
            </p>
          </div>
        </GlowCard>
      </section>

      {/* ------------------------------------------------------- Architecture */}
      <section>
        <SectionTitle
          title="Where each piece runs"
          hint="Two Lambdas, one table, no VPC, no gateway, no container registry. Every choice here is bounded by the always-free tier and enforced by a CI cost gate."
        />
        <Card className="overflow-x-auto p-6">
          <pre className="min-w-[640px] font-mono text-[12.5px] leading-[1.85] text-ink-300">
{`  agent (Lambda, Groq)        ─┐
  agent (laptop, Ollama)      ─┤ HTTPS + x-api-key
  any MCP client via proxy    ─┘
                                │
                                ▼
                    ┌────────────────────────────┐
                    │  Guardrail control plane   │  FastAPI on Lambda (arm64, 512 MB)
                    │  ┌──────────────────────┐  │  Function URL — no API Gateway
                    │  │ guardrail-core       │  │  ($1/M after 12 months; this is $0)
                    │  │ pure policy engine   │  │
                    │  │ no I/O, no LLM       │  │
                    │  └──────────────────────┘  │
                    └─────────────┬──────────────┘
                                  │  PutItem / Query / UpdateItem
                                  │  deliberately NO DeleteItem
                                  ▼
                    DynamoDB  guardrail-audit-dev
                    single table · PROVISIONED 5/5 + two GSIs
                    15 of the free 25 WCU/RCU
                    hash chain · PITR on · TTL for held decisions`}
          </pre>
          <div className="mt-5 grid gap-4 border-t border-ink-800 pt-5 sm:grid-cols-3">
            <KeyValue
              label="Why no API Gateway"
              value="Its 1M requests/month is a 12-month offer, then $1/M. A Function URL has no per-request charge, ever."
            />
            <KeyValue
              label="Why provisioned DynamoDB"
              value="On-demand has no free tier at all and bills from request one. Autoscaling is off, so a spike throttles instead of billing."
            />
            <KeyValue
              label="Why no DeleteItem"
              value="A governance system whose IAM role can erase its own evidence is much weaker. Held decisions expire by TTL instead."
            />
          </div>
        </Card>
      </section>
    </div>
  );
}
