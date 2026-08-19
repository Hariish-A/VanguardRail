/**
 * System Health — liveness, readiness, and the constraints this thing runs inside.
 *
 * `/healthz` and `/readyz` are deliberately different, and this page keeps them apart.
 * Liveness answers "is the process running" and performs no I/O on purpose: a liveness
 * probe that touches a database reports healthy processes as dead during a dependency
 * outage and gets them restarted, which makes the outage worse. Readiness answers "can it
 * serve a decision" and returns 503 when it cannot.
 *
 * The capacity panel is here because in this system the ceilings are a *design output*,
 * not an operational detail. The service is pinned to the AWS always-free tier, so
 * "15 of 25 write units" and "10 of 10 custom metrics" are the reason certain features
 * do not exist — and a page that showed throughput without showing the ceiling would be
 * implying scale the deployment does not have.
 */

import { motion } from "framer-motion";
import { api } from "@/lib/api";
import { BUILD_VERSION } from "@/lib/api";
import { useAsync, useSession } from "@/lib/store";
import { GlowCard, ShimmerBorder } from "@/components/effects";
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

/** A used-of-budget bar. `full` colours it amber, because full means "spending next". */
function Meter({
  label,
  used,
  total,
  unit,
  note,
}: {
  label: string;
  used: number;
  total: number;
  unit: string;
  note: string;
}) {
  const share = Math.min(1, used / total);
  const full = used >= total;

  return (
    <div className="rounded-xl border border-ink-800 bg-ink-900/40 p-4">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-[13px] text-ink-200">{label}</span>
        <span
          className={`font-mono text-[13px] ${full ? "text-hitl" : "text-ink-300"}`}
        >
          {used}/{total} {unit}
        </span>
      </div>
      <div className="mt-2.5 h-1.5 overflow-hidden rounded-full bg-ink-800">
        <motion.div
          initial={{ width: 0 }}
          whileInView={{ width: `${share * 100}%` }}
          viewport={{ once: true }}
          transition={{ duration: 0.8, ease: "easeOut" }}
          className="h-full rounded-full"
          style={{
            background: full ? "var(--color-hitl)" : "var(--color-brand-500)",
          }}
        />
      </div>
      <p className="mt-2 text-[12px] leading-relaxed text-ink-500">{note}</p>
    </div>
  );
}

const BUDGETS = [
  {
    label: "DynamoDB write capacity",
    used: 15,
    total: 25,
    unit: "WCU",
    note: "Table 5/5 plus two GSIs at 5/5. Provisioned, never on-demand — on-demand has no free tier and bills from request one. Autoscaling is off, so a spike throttles instead of billing.",
  },
  {
    label: "CloudWatch custom metrics",
    used: 10,
    total: 10,
    unit: "series",
    note: "Exactly full. Each name+dimension pair counts separately, so adding any metric now costs money — a new one must displace an existing one. Anything finer-grained goes to structured logs and is queried with Logs Insights.",
  },
  {
    label: "CloudWatch alarms",
    used: 7,
    total: 10,
    unit: "alarms",
    note: "Standard resolution. Every one routes to an SNS topic; an email subscription is added only when an address is configured, because an alarm nobody receives manufactures false confidence.",
  },
  {
    label: "Lambda concurrency",
    used: 10,
    total: 10,
    unit: "containers",
    note: "The account quota, which is also the ceiling. Reserved concurrency cannot be set here at all: AWS requires 10 to stay unreserved, so the maximum reservable value is zero.",
  },
];

const FACTS = [
  {
    title: "Fail closed, always",
    body: "If the guardrail is unreachable the SDK blocks rather than proceeds. That trades availability for safety on purpose: an attacker who can saturate the guardrail can stop governed agents from acting, and that is the correct failure for a control whose job is to prevent unrecorded actions.",
  },
  {
    title: "Rate limiting is per container",
    body: "The token bucket is in-process, so the real global bound is containers × per-container rate — not a fair-share mechanism, and not described as one. It costs zero DynamoDB capacity, which is why it is in-process.",
  },
  {
    title: "Throttled writes are retried, not dropped",
    body: "A DynamoDB write throttle is a first-class retryable failure: jittered backoff bounded by a 5-second deadline inside the 10-second Lambda timeout, with botocore's own retries capped so they cannot compound. Before that, a throttle escaped as an unhandled 500 with no log line.",
  },
  {
    title: "A policy-store outage is not an agent outage",
    body: "Hot reload re-checks the active pointer on the request path every 30 seconds. If the store is unreachable, the last known good bundle keeps serving rather than requests failing — and /readyz reports the degradation instead of hiding it.",
  },
  {
    title: "Sustained throughput is ~6 req/s, not 18",
    body: "A 30-second load test reported 18 req/s with zero errors. That was DynamoDB burst credit, not capacity. Runs shorter than about 120 seconds lie here, and the committed report states the sustainable figure.",
  },
  {
    title: "The same image runs off Lambda",
    body: "The FastAPI app is byte-identical in the zip and the container; only the entrypoint differs. The conformance suite is run against a real container in CI, so portability is executed rather than asserted — it was asserted for four milestones while the image did not actually start.",
  },
];

export function SystemHealthPage() {
  const { session, status } = useSession();
  const connected = status === "connected";

  const health = useAsync(
    () => (connected ? api.health(session) : Promise.resolve(null)),
    [connected, session.baseUrl],
  );
  const ready = useAsync(
    () => (connected ? api.readiness(session) : Promise.resolve(null)),
    [connected, session.baseUrl],
  );
  const policies = useAsync(
    () => (connected ? api.policies(session) : Promise.resolve(null)),
    [connected, session.baseUrl, session.apiKey],
  );

  if (!connected) {
    return (
      <EmptyState
        icon="♥"
        title="Not connected"
        detail="Health and readiness are unauthenticated on the deployed service, but this page also reads the policy state, which is not."
        action={<Button onClick={() => (window.location.hash = "/connect")}>Connect</Button>}
      />
    );
  }

  const isReady = ready.data?.ready ?? false;

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-[26px] font-semibold tracking-tight text-ink-100">
            System Health
          </h1>
          <p className="mt-2 max-w-3xl text-[14px] leading-relaxed text-ink-400">
            What is running, whether it can actually serve a decision, and the ceilings it
            is designed to live inside.
          </p>
        </div>
        <Button
          size="sm"
          variant="outline"
          onClick={() => {
            health.reload();
            ready.reload();
            policies.reload();
          }}
        >
          Refresh
        </Button>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <GlowCard className="p-5" accent="var(--color-allow)">
          <ShimmerBorder />
          <SectionTitle
            title="Liveness — /healthz"
            hint="Performs no I/O by design. A liveness probe that touches a database gets healthy processes restarted during an outage."
          />
          {health.loading ? (
            <Skeleton className="h-16 w-full" />
          ) : health.error ? (
            <ErrorNote error={health.error} />
          ) : health.data ? (
            <div className="grid gap-4 sm:grid-cols-3">
              <KeyValue
                label="Status"
                value={
                  <span className="font-mono text-allow">
                    {health.data.status}
                  </span>
                }
              />
              <KeyValue label="Stage" value={health.data.stage} mono />
              <KeyValue
                label="Container uptime"
                value={`${health.data.uptime_seconds.toFixed(0)}s`}
                mono
              />
              <div className="sm:col-span-3">
                <KeyValue
                  label="Deployed commit"
                  value={health.data.version}
                  mono
                  title={health.data.version}
                />
              </div>
            </div>
          ) : null}
        </GlowCard>

        <GlowCard
          className="p-5"
          accent={isReady ? "var(--color-allow)" : "var(--color-block)"}
        >
          <SectionTitle
            title="Readiness — /readyz"
            hint="Enumerates dependencies rather than collapsing to a boolean, and returns 503 when one is down."
          />
          {ready.loading ? (
            <Skeleton className="h-16 w-full" />
          ) : ready.error ? (
            <ErrorNote error={ready.error} />
          ) : ready.data ? (
            <div className="space-y-3">
              <div className="flex items-center gap-2.5">
                <span
                  className="h-2 w-2 rounded-full"
                  style={{
                    background: isReady ? "var(--color-allow)" : "var(--color-block)",
                  }}
                />
                <span
                  className="font-mono text-[13px]"
                  style={{ color: isReady ? "var(--color-allow)" : "var(--color-block)" }}
                >
                  {isReady ? "ready" : "NOT READY — 503"}
                </span>
              </div>
              {ready.data.dependencies.map((dependency) => (
                <div
                  key={dependency.name}
                  className="rounded-lg border border-ink-800 bg-ink-900/40 p-3"
                >
                  <div className="flex items-center gap-2">
                    <span
                      className="h-1.5 w-1.5 rounded-full"
                      style={{
                        background: dependency.ready
                          ? "var(--color-allow)"
                          : "var(--color-block)",
                      }}
                    />
                    <span className="font-mono text-[12.5px] text-ink-200">
                      {dependency.name}
                    </span>
                  </div>
                  <p className="mt-1.5 text-[12px] leading-relaxed text-ink-400">
                    {dependency.detail}
                  </p>
                </div>
              ))}
            </div>
          ) : null}
          <p className="mt-4 border-t border-ink-800 pt-3 text-[12px] leading-relaxed text-ink-500">
            This probe used to hard-code <span className="font-mono">ready: true</span> for
            three placeholder tables, so it could never return 503. A check that cannot
            fail is worse than no check — it reads as coverage. It now has a test that
            deliberately breaks a dependency and requires the probe to notice.
          </p>
        </GlowCard>
      </div>

      <section>
        <SectionTitle
          title="Policy in force"
          hint="Hot-reloaded on the request path, so activating a version changes live behaviour without a redeploy."
        />
        {policies.loading ? (
          <Skeleton className="h-24 w-full" />
        ) : policies.error ? (
          <ErrorNote error={policies.error} />
        ) : policies.data ? (
          <Card className="p-5">
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <KeyValue
                label="Active version"
                value={`v${policies.data.active_version ?? 1}`}
                mono
              />
              <KeyValue label="Source" value={policies.data.active_source} mono />
              <KeyValue label="Bundle" value={policies.data.bundle_id} mono />
              <KeyValue
                label="Published versions"
                value={String(policies.data.versions.length)}
                mono
              />
            </div>
            {policies.data.degraded && (
              <p className="mt-4 rounded-lg border border-[color-mix(in_oklab,var(--color-hitl)_38%,transparent)] bg-[color-mix(in_oklab,var(--color-hitl)_8%,transparent)] p-3 text-[12.5px] leading-relaxed text-hitl">
                Degraded — the policy store is unreachable, so the last known good bundle
                is still being served. Agents keep being governed; the trade-off is
                deliberate and is that a cold start during the outage would fall back to
                the bundle packaged with the build, which may be more permissive.
              </p>
            )}
          </Card>
        ) : null}
      </section>

      <section>
        <SectionTitle
          title="Free-tier budgets, and what they cost in features"
          hint="These ceilings are design inputs. Two of the four are exactly full, which is why certain things in this system are logs rather than metrics."
        />
        <div className="grid gap-3 sm:grid-cols-2">
          {BUDGETS.map((budget) => (
            <Meter key={budget.label} {...budget} />
          ))}
        </div>
        <p className="mt-4 text-[12.5px] leading-relaxed text-ink-500">
          A CI gate fails the build if a synthesized template contains a NAT gateway, load
          balancer, Fargate task, ECR repository, WAF ACL, Secrets Manager secret, or an
          on-demand DynamoDB table. Cost discipline is enforced by the pipeline rather than
          by memory.
        </p>
      </section>

      <section>
        <SectionTitle
          title="Behaviour under stress, stated plainly"
          hint="The parts most systems leave to a footnote."
        />
        <div className="grid gap-3 lg:grid-cols-2">
          {FACTS.map((fact, index) => (
            <motion.div
              key={fact.title}
              initial={{ opacity: 0, y: 10 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: "-50px" }}
              transition={{ delay: index * 0.05, duration: 0.35 }}
            >
              <Card className="h-full p-5">
                <h3 className="text-[13.5px] font-semibold text-ink-100">{fact.title}</h3>
                <p className="mt-2 text-[13px] leading-relaxed text-ink-400">{fact.body}</p>
              </Card>
            </motion.div>
          ))}
        </div>
      </section>

      <Card className="p-5">
        <div className="flex flex-wrap items-center gap-4">
          <Badge tone="brand">console build</Badge>
          <span className="font-mono text-[12px] text-ink-400">{BUILD_VERSION}</span>
          <span className="text-[12px] text-ink-500">
            The deployed commit above is the control plane's. If the two differ, the
            console was built from a different revision than the service it is talking to.
          </span>
        </div>
      </Card>
    </div>
  );
}
