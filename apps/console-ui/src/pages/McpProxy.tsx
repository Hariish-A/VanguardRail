/**
 * MCP Proxy — governing a tool server that knows nothing about Guardrail.
 *
 * ## Why this page is part explanation and part live run
 *
 * The proxy is a **stdio** process: it spawns the upstream MCP server as its own child
 * and sits between the agent and it. A browser cannot drive that, and pretending
 * otherwise would be theatre. So this page does two honest things:
 *
 * * runs the four MCP scenarios against the live control plane, which is the part that
 *   *is* remotely verifiable — the policy decisions the proxy enforces;
 * * explains the mechanism and points at `scripts/mcp_demo.py`, which runs the real
 *   `@modelcontextprotocol/server-filesystem` end to end with a leak canary.
 *
 * The distinction matters. What runs here proves the **policy** is right. What proves the
 * **proxy** is right is the canary in that script, and this page says which is which
 * rather than letting a green tick imply both.
 *
 * ## Why the proxy is the strongest enforcement point in the system
 *
 * Everywhere else, enforcement depends on the agent calling the SDK. An agent that
 * bypasses it is ungoverned — that is the residual risk named first in the threat model.
 * The proxy removes the choice: it *is* the transport to the upstream server, so there is
 * no path to the tools that does not pass through policy.
 */

import { useState } from "react";
import { ALL_SCENARIOS, runCorpus, verdict, type ScenarioResult } from "@/lib/conformance";
import { cn, prettyJson } from "@/lib/format";
import { useSession } from "@/lib/store";
import { DecisionBadge } from "@/components/DecisionBadge";
import { GlowCard } from "@/components/effects";
import {
  Badge,
  Button,
  Card,
  CodeBlock,
  EmptyState,
  ErrorNote,
  SectionTitle,
} from "@/components/ui";

const MCP_SCENARIOS = ALL_SCENARIOS.filter((s) => s.id.startsWith("mcp-"));

export function McpProxyPage() {
  const { session, status } = useSession();
  const connected = status === "connected";

  const [results, setResults] = useState<ScenarioResult[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const run = async () => {
    setRunning(true);
    setError(null);
    setResults([]);
    try {
      const collected: ScenarioResult[] = [];
      await runCorpus(session, MCP_SCENARIOS, {
        onResult: (r) => {
          collected.push(r);
          setResults([...collected]);
        },
      });
    } catch (cause) {
      setError(cause);
    } finally {
      setRunning(false);
    }
  };

  const passed = results.filter((r) => verdict(r) === "pass").length;
  const allGreen = results.length === MCP_SCENARIOS.length && passed === results.length;

  return (
    <div className="space-y-7">
      <div>
        <h1 className="text-[26px] font-semibold tracking-tight text-ink-100">MCP Proxy</h1>
        <p className="mt-2 max-w-3xl text-[14px] leading-relaxed text-ink-400">
          <span className="font-mono text-ink-300">guardrail-mcp</span> fronts any Model
          Context Protocol server and evaluates every{" "}
          <span className="font-mono">tools/call</span> before it is forwarded — with{" "}
          <strong className="text-ink-200">no changes to that server</strong>. It is how a
          third-party agent gets governed without anyone editing its code.
        </p>
      </div>

      {/* ------------------------------------------------------------ mechanism */}
      <Card className="overflow-x-auto p-6">
        <SectionTitle
          title="Where it sits"
          hint="The proxy spawns the upstream server as its own child process, so it is the only path to those tools."
        />
        <pre className="min-w-[620px] font-mono text-[12.5px] leading-[1.85] text-ink-300">
{`  agent / IDE / Claude Desktop
        │  MCP over stdio — the agent thinks it is talking to the server
        ▼
  ┌──────────────────────────────────┐
  │  guardrail-mcp                   │   tools/list  → forwarded unchanged
  │                                  │   tools/call  → evaluated FIRST
  │    ├── HTTPS ──► control plane   │
  │    │            allow / block /  │
  │    │            require_hitl     │
  │    │                             │
  │    └── spawns ──► upstream MCP server (unmodified)
  └──────────────────────────────────┘
                     e.g. npx @modelcontextprotocol/server-filesystem

  A blocked call never reaches the child. The agent receives a structured
  refusal naming the rule, so it explains the denial instead of crashing.`}
        </pre>
      </Card>

      {/* ---------------------------------------------------------- live policy */}
      {!connected ? (
        <EmptyState
          icon="⇉"
          title="Connect to run the MCP policy scenarios"
          detail="The four scenarios below run against the live control plane — the same decisions the proxy enforces locally."
          action={<Button onClick={() => (window.location.hash = "/connect")}>Connect</Button>}
        />
      ) : (
        <>
          <Card className="p-5">
            <SectionTitle
              title="The policy the proxy enforces"
              hint="These four run live. They verify the decisions — see below for what verifies the proxy itself."
            />
            <div className="flex flex-wrap items-center gap-4">
              <Button loading={running} disabled={running} onClick={() => void run()}>
                Run the {MCP_SCENARIOS.length} MCP scenarios
              </Button>
              {results.length > 0 && (
                <span
                  className={cn(
                    "font-mono text-[13px]",
                    allGreen ? "text-allow" : "text-ink-300",
                  )}
                >
                  {passed}/{results.length} passed
                </span>
              )}
            </div>
          </Card>

          <ErrorNote error={error} />

          {results.length > 0 && (
            <div className="space-y-2">
              {results.map((result) => {
                const state = verdict(result);
                return (
                  <Card
                    key={result.scenario.id}
                    className={cn(
                      "p-4",
                      state !== "pass" &&
                        "border-[color-mix(in_oklab,var(--color-block)_45%,transparent)]",
                    )}
                  >
                    <div className="flex flex-wrap items-center gap-3">
                      <span
                        className={cn(
                          "h-2 w-2 rounded-full",
                          state === "pass" ? "bg-allow" : "bg-block",
                        )}
                      />
                      <span className="font-mono text-[12.5px] text-ink-100">
                        {result.scenario.action.tool}
                      </span>
                      {result.observed && (
                        <DecisionBadge effect={result.observed.decision} size="sm" />
                      )}
                      <span className="ml-auto font-mono text-[11px] text-ink-600">
                        {result.observed?.ruleIds.join(", ") || "no rules"}
                      </span>
                    </div>
                    <p className="mt-1.5 text-[12.5px] leading-relaxed text-ink-400">
                      {result.scenario.description}
                    </p>
                    {result.failures.map((failure, index) => (
                      <p key={index} className="mt-1 font-mono text-[12px] text-block">
                        · {failure}
                      </p>
                    ))}
                    {result.error && (
                      <p className="mt-1 font-mono text-[12px] text-hitl">
                        request failed: {result.error}
                      </p>
                    )}
                  </Card>
                );
              })}
            </div>
          )}
        </>
      )}

      {/* ------------------------------------------------------------- canary */}
      <GlowCard className="p-6" accent="var(--color-block)">
        <div className="flex flex-wrap items-center gap-3">
          <Badge tone="bad">the part a browser cannot prove</Badge>
        </div>
        <h3 className="mt-3 text-[18px] font-semibold leading-snug text-ink-100">
          The leak canary: proving the request never arrived
        </h3>
        <p className="mt-3 max-w-3xl text-[13.5px] leading-relaxed text-ink-300">
          A decision of <span className="font-mono">block</span> is a claim the proxy makes
          about itself. The scenarios above verify the <em>policy</em> is right; they cannot
          verify the proxy <em>obeyed</em> it. So{" "}
          <span className="font-mono text-ink-200">scripts/mcp_demo.py</span> runs the real
          `@modelcontextprotocol/server-filesystem` against a directory containing a private
          key file with a unique canary string inside it, asks for that file through the
          proxy, and then asserts the canary appears{" "}
          <strong className="text-ink-100">nowhere in the entire transcript</strong>.
        </p>
        <p className="mt-3 max-w-3xl text-[13px] leading-relaxed text-ink-400">
          That is the difference between "the proxy said no" and "the bytes never left the
          disk". An ordinary read still succeeds in the same run, and a write is held for
          review with the file verifiably untouched — so the demo also shows the proxy is
          not simply refusing everything, which would pass a naive block test.
        </p>
        <CodeBlock
          className="mt-4"
          code={`# Needs node/npx. Runs an unmodified third-party MCP server end to end.
uv run python scripts/mcp_demo.py

# Or put the proxy in front of any MCP server yourself:
uv run guardrail-mcp --server filesystem --endpoint "$BASE" \\
  -- npx -y @modelcontextprotocol/server-filesystem /some/dir`}
        />
      </GlowCard>

      {/* --------------------------------------------------------- why it matters */}
      <div className="grid gap-4 lg:grid-cols-2">
        <Card className="p-5">
          <SectionTitle title="Why this is the strongest enforcement point" />
          <p className="text-[13px] leading-relaxed text-ink-300">
            Everywhere else in this system, enforcement depends on the agent calling the
            SDK. An agent that simply does not is ungoverned — the first residual risk in
            the threat model, and an honest one: a firewall does not inspect a cable that
            bypasses it.
          </p>
          <p className="mt-2.5 text-[13px] leading-relaxed text-ink-400">
            The proxy removes the choice. It runs the upstream server as its own child
            process over stdio, so it <em>is</em> the transport. There is no path to those
            tools that does not pass through policy, and the agent needs no knowledge that
            any of this is happening.
          </p>
        </Card>

        <Card className="p-5">
          <SectionTitle title="Two bugs this cost, both worth knowing" />
          <ul className="space-y-2.5 text-[13px] leading-relaxed text-ink-300">
            <li>
              · <strong className="text-ink-100">A stdio deadlock.</strong> Iterating the
              child's stream with <span className="font-mono">for line in stream</span>{" "}
              buffers, so the proxy waited for a block that the server would only send after
              a reply it was waiting for. <span className="font-mono">readline()</span>{" "}
              fixed it, plus closing the upstream stdin on EOF.
            </li>
            <li>
              · <strong className="text-ink-100">WinError 193 spawning npx.</strong> On
              Windows the executable needs resolving through PATHEXT before{" "}
              <span className="font-mono">subprocess</span> will start it.
            </li>
          </ul>
          <p className="mt-3 text-[12.5px] leading-relaxed text-ink-500">
            Both only appear when the thing is actually run against a real server, which is
            the argument for `mcp_demo.py` existing at all.
          </p>
        </Card>
      </div>

      <Card className="p-5">
        <SectionTitle
          title="How the rules are scoped"
          hint="Tools are namespaced by server, so a rule written for one proxied server cannot silently govern another."
        />
        <CodeBlock
          code={prettyJson({
            tool: "mcp.filesystem.read_file",
            note: "mcp.<server>.<tool> — the server name comes from --server",
            "rule mcp-credential-read-block": "matches only mcp.filesystem.*",
            "scenario mcp-server-prefix-scopes-the-rule":
              "asserts the same path through a different server does NOT match",
          })}
        />
        <p className="mt-3 text-[12.5px] leading-relaxed text-ink-500">
          The last of those four scenarios exists precisely to stop the rule being broader
          than intended. A rule that accidentally governs every proxied server would look
          like extra safety and would in fact be an untested blast radius.
        </p>
      </Card>
    </div>
  );
}
