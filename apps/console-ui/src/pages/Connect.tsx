/**
 * The connection screen.
 *
 * ## Why you paste a key rather than sign in
 *
 * There is no login form because there is no user database. Authentication is a hashed
 * API key checked in constant time inside the application — the Lambda Function URL is
 * public by design (a new AWS account cannot create a CloudFront distribution without a
 * support case), so auth had to live somewhere it could be tested and audited.
 *
 * That makes the credential a **personal access token**: it belongs to the person
 * holding it, it is never embedded in this bundle, and it lives in `sessionStorage`
 * until the tab closes. The honest limitation is that an XSS on this page could read it.
 * Cognito hosted sign-in is the designed upgrade and is deliberately deferred — see
 * `docs/threat-model.md`, gap 6.
 *
 * ## Why the role matters here
 *
 * The key's role decides what the rest of the console will even render. A key with the
 * `agent` role can evaluate and read, and **cannot approve** — because an agent that can
 * approve the action its own policy held has not been governed at all. That was a real
 * defect in this system, found and closed; see the Overview.
 */

import { useState } from "react";
import { DEFAULT_AGENT_URL, DEFAULT_BASE_URL } from "@/lib/api";
import { useSession } from "@/lib/store";
import { Aurora, GlowCard, GridField, ShimmerBorder } from "@/components/effects";
import {
  Badge,
  Button,
  Card,
  ErrorNote,
  Field,
  Input,
  SectionTitle,
} from "@/components/ui";
import { Logo } from "@/components/Shell";

const ROLE_TABLE = [
  {
    role: "agent",
    grants: "evaluate · simulate · read audit, queue, policy",
    denies: "cannot approve · cannot change policy",
    tone: "neutral" as const,
  },
  {
    role: "reviewer",
    grants: "everything above, plus resolving held actions",
    denies: "cannot change policy",
    tone: "warn" as const,
  },
  {
    role: "admin",
    grants: "everything above, plus publishing and activating policy",
    denies: "—",
    tone: "bad" as const,
  },
];

export function ConnectPage({ standalone = false }: { standalone?: boolean }) {
  const { session, connect, disconnect, status, error, identity } = useSession();

  const [baseUrl, setBaseUrl] = useState(session.baseUrl || DEFAULT_BASE_URL);
  const [apiKey, setApiKey] = useState(session.apiKey);
  const [agentUrl, setAgentUrl] = useState(session.agentUrl || DEFAULT_AGENT_URL);
  const [agentKey, setAgentKey] = useState(session.agentKey);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    await connect({ baseUrl, apiKey, agentUrl, agentKey });
  };

  return (
    <div className={standalone ? "relative min-h-screen" : "relative"}>
      {standalone && (
        <>
          <Aurora />
          <GridField />
        </>
      )}

      <div
        className={
          standalone
            ? "mx-auto flex min-h-screen max-w-5xl flex-col justify-center px-5 py-16"
            : ""
        }
      >
        {standalone && (
          <div className="mb-8 flex items-center justify-between">
            <Logo />
            <Badge tone="brand">PS-3.1 · action guardrail</Badge>
          </div>
        )}

        <div className="grid gap-6 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
          <GlowCard className="p-6">
            <ShimmerBorder />
            <SectionTitle
              title={standalone ? "Connect to the control plane" : "Connection"}
              hint="The key is sent as x-api-key, held only in this tab's sessionStorage, and verified against /v1/me before anything is stored."
            />

            <form onSubmit={submit} className="space-y-4">
              <Field
                label="Control plane base URL"
                hint="The deployed Lambda Function URL. No trailing slash needed."
              >
                <Input
                  value={baseUrl}
                  onChange={(event) => setBaseUrl(event.target.value)}
                  placeholder="https://<control-plane>.lambda-url.us-east-1.on.aws"
                  autoComplete="off"
                  spellCheck={false}
                  className="font-mono text-[12.5px]"
                />
              </Field>

              <Field
                label="API key"
                hint="Never baked into this bundle — a deployed frontend is a public artifact. Mint one with scripts/generate_api_key.py."
              >
                <Input
                  type="password"
                  value={apiKey}
                  onChange={(event) => setApiKey(event.target.value)}
                  placeholder="paste your key"
                  autoComplete="off"
                  className="font-mono text-[12.5px]"
                />
              </Field>

              <div className="rounded-xl border border-ink-700/70 bg-ink-900/40 p-4">
                <p className="mb-3 text-[12px] uppercase tracking-wider text-ink-500">
                  Optional — the AWS-hosted demo agent
                </p>
                <div className="space-y-3">
                  <Field
                    label="Agent URL"
                    hint="A separate Lambda. It is an ordinary SDK consumer with its own credential — the same boundary any third-party agent would sit behind."
                  >
                    <Input
                      value={agentUrl}
                      onChange={(event) => setAgentUrl(event.target.value)}
                      placeholder="https://<agent>.lambda-url.us-east-1.on.aws"
                      autoComplete="off"
                      className="font-mono text-[12.5px]"
                    />
                  </Field>
                  <Field label="Agent key">
                    <Input
                      type="password"
                      value={agentKey}
                      onChange={(event) => setAgentKey(event.target.value)}
                      placeholder="paste the agent key"
                      autoComplete="off"
                      className="font-mono text-[12.5px]"
                    />
                  </Field>
                </div>
              </div>

              <ErrorNote error={error} />

              <div className="flex flex-wrap items-center gap-3 pt-1">
                <Button type="submit" loading={status === "connecting"}>
                  {status === "connected" ? "Reconnect" : "Connect"}
                </Button>
                {status === "connected" && (
                  <Button type="button" variant="outline" onClick={disconnect}>
                    Disconnect
                  </Button>
                )}
                {identity && (
                  <span className="font-mono text-[12px] text-ink-400">
                    connected as {identity.key_id} · {identity.role} · tenant{" "}
                    {identity.tenant_id}
                  </span>
                )}
              </div>
            </form>
          </GlowCard>

          <div className="space-y-4">
            <Card className="p-5">
              <h3 className="text-[13px] font-semibold text-ink-100">
                Why paste a key instead of signing in?
              </h3>
              <p className="mt-2 text-[13px] leading-relaxed text-ink-400">
                There is no user database. Authentication is a SHA-256 key digest compared
                in constant time <em>inside</em> the application, because the endpoint is a
                public Lambda Function URL — CloudFront, which would have hidden the
                origin, cannot be created on an unverified AWS account.
              </p>
              <p className="mt-2 text-[13px] leading-relaxed text-ink-400">
                So the credential is a personal access token. It is never in this bundle,
                never in the repository, and dies with the tab. An XSS here could read it;
                Cognito sign-in is the designed upgrade and is deferred on purpose rather
                than forgotten.
              </p>
            </Card>

            <Card className="p-5">
              <h3 className="text-[13px] font-semibold text-ink-100">
                What your key's role decides
              </h3>
              <div className="mt-3 space-y-2.5">
                {ROLE_TABLE.map((row) => (
                  <div
                    key={row.role}
                    className="rounded-lg border border-ink-700/60 bg-ink-900/40 p-3"
                  >
                    <Badge tone={row.tone}>{row.role}</Badge>
                    <p className="mt-2 text-[12.5px] leading-relaxed text-ink-300">
                      {row.grants}
                    </p>
                    <p className="mt-1 text-[12px] leading-relaxed text-ink-500">
                      {row.denies}
                    </p>
                  </div>
                ))}
              </div>
              <p className="mt-3 text-[12.5px] leading-relaxed text-ink-500">
                An unrecognised role, or none at all, is treated as{" "}
                <span className="font-mono text-ink-300">agent</span>. The default has to
                restrict rather than grant — a key table that forgets a role must not hand
                out approval rights.
              </p>
            </Card>
          </div>
        </div>
      </div>
    </div>
  );
}
