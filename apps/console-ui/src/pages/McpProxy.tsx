/**
 * MCP Proxy — governing a tool server that knows nothing about Vanguardrail.
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
import { cn } from "@/lib/format";
import { useSession } from "@/lib/store";
import { DecisionBadge } from "@/components/DecisionBadge";
import {
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
      </div>

      {/* ------------------------------------------------------------ mechanism */}
      <Card className="overflow-x-auto p-6">
        <SectionTitle
          title="Where it sits"
          hint="The proxy runs the upstream server as its own child process."
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

      {/* The proxy itself runs over stdio and cannot be driven from a browser. This is
          how to run it, and the script that verifies it end to end. */}
      <Card className="p-5">
        <SectionTitle
          title="Run the proxy"
          hint="Requires node/npx. The proxy runs locally; the control plane it consults is the deployed one."
        />
        <CodeBlock
          code={`# End-to-end check against a real third-party MCP server,
# including a canary asserting a blocked read never reached it.
uv run python scripts/mcp_demo.py

# Front any MCP server:
uv run guardrail-mcp --server filesystem --endpoint "$GUARDRAIL_BASE_URL" \
  -- npx -y @modelcontextprotocol/server-filesystem /some/dir`}
        />
      </Card>

      {/* --------------------------------------------------------- why it matters */}
    </div>
  );
}
