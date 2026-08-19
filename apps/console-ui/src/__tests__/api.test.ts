/**
 * The client, and the one rule that matters: a credential is stored only after the
 * server accepts it.
 *
 * A console that persists an unverified key looks connected and is not — every
 * subsequent page then renders an empty table, which reads as "no activity" rather than
 * "you are not authenticated". For a tool whose job is telling you whether something is
 * being governed, that is the worst possible failure mode.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  agentApi,
  api,
  clearSession,
  loadSession,
  saveSession,
  type Session,
} from "@/lib/api";

const SESSION: Session = {
  baseUrl: "https://guardrail.example",
  apiKey: "secret-key",
  agentUrl: "https://agent.example",
  agentKey: "agent-key",
};

function mockFetch(
  handler: (url: string, init: RequestInit | undefined) => Response,
): void {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => Promise.resolve(handler(url, init))),
  );
}

function json(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

beforeEach(() => {
  vi.unstubAllGlobals();
  clearSession();
});

// ---------------------------------------------------------------------------
// Session storage
// ---------------------------------------------------------------------------

describe("session storage", () => {
  it("round-trips a session", () => {
    saveSession(SESSION);

    expect(loadSession()).toMatchObject({
      baseUrl: "https://guardrail.example",
      apiKey: "secret-key",
    });
  });

  it("strips trailing slashes so paths never double up", () => {
    saveSession({ ...SESSION, baseUrl: "https://guardrail.example///" });

    expect(loadSession().baseUrl).toBe("https://guardrail.example");
  });

  it("clears everything on disconnect", () => {
    saveSession(SESSION);

    clearSession();

    expect(loadSession().apiKey).toBe("");
  });
});

// ---------------------------------------------------------------------------
// Request shape
// ---------------------------------------------------------------------------

describe("requests", () => {
  it("sends the key as x-api-key and never in the URL", async () => {
    let seenUrl = "";
    let seenHeaders: Record<string, string> = {};
    mockFetch((url, init) => {
      seenUrl = url;
      seenHeaders = (init?.headers ?? {}) as Record<string, string>;
      return json({ key_id: "k", tenant_id: "t", name: "n", role: "agent", capabilities: [], stage: "dev", version: "abc" });
    });

    await api.me(SESSION);

    expect(seenUrl).toBe("https://guardrail.example/v1/me");
    expect(seenUrl).not.toContain("secret-key");
    expect(seenHeaders["x-api-key"]).toBe("secret-key");
  });

  it("refuses to call anything without a base URL", async () => {
    await expect(api.me({ ...SESSION, baseUrl: "" })).rejects.toBeInstanceOf(ApiError);
  });

  it("passes the audit filter through as a query parameter", async () => {
    let seenUrl = "";
    mockFetch((url) => {
      seenUrl = url;
      return json({ entries: [], count: 0, tenant_id: "t" });
    });

    await api.audit(SESSION, 25, "block");

    expect(seenUrl).toContain("limit=25");
    expect(seenUrl).toContain("effect=block");
  });

  it("wraps the action envelope for simulate but not for evaluate", async () => {
    const bodies: string[] = [];
    mockFetch((_url, init) => {
      bodies.push(String(init?.body));
      return json({});
    });

    const action = {
      agent_id: "a",
      session_id: "s",
      tool: "file.read",
      arguments: {},
    };
    await api.evaluate(SESSION, action);
    await api.simulate(SESSION, action);

    expect(JSON.parse(bodies[0])).toMatchObject({ tool: "file.read" });
    expect(JSON.parse(bodies[1])).toMatchObject({ action: { tool: "file.read" } });
  });
});

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

describe("errors", () => {
  it("carries the status, the server's detail, and the request id", async () => {
    mockFetch(() =>
      json({ detail: "role 'agent' may not approve" }, 403, {
        "x-request-id": "req-123",
      }),
    );

    const error = await api
      .resolve(SESSION, "d1", true, "because")
      .catch((cause: unknown) => cause);

    expect(error).toBeInstanceOf(ApiError);
    const api_error = error as ApiError;
    expect(api_error.status).toBe(403);
    expect(api_error.detail).toContain("may not approve");
    expect(api_error.requestId).toBe("req-123");
  });

  it("gives distinct guidance per status, because they imply different next steps", () => {
    const statuses = [0, 401, 403, 404, 429, 503];
    const guidance = statuses.map(
      (status) => new ApiError(status, "", null, "/x").guidance,
    );

    expect(new Set(guidance).size).toBe(statuses.length);
    for (const text of guidance) expect(text.length).toBeGreaterThan(20);
  });

  it("says 503 means the guardrail is failing closed, not merely that it is slow", () => {
    // The distinction is the whole product. A 503 here means governed agents are being
    // refused rather than let through — an operator reading "try again later" would
    // draw exactly the wrong conclusion.
    expect(new ApiError(503, "", null, "/x").guidance).toMatch(/fails closed/i);
  });

  it("reports an unreachable host rather than inventing a status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))),
    );

    const error = (await api.me(SESSION).catch((cause: unknown) => cause)) as ApiError;

    expect(error.status).toBe(0);
    // A CORS rejection and a dead host are indistinguishable from JavaScript. Naming
    // both is honest; guessing one would send an operator down the wrong path.
    expect(error.guidance).toMatch(/CORS/);
  });

  it("does not choke on a non-JSON error body", async () => {
    mockFetch(() => new Response("<html>502 Bad Gateway</html>", { status: 502 }));

    const error = (await api.me(SESSION).catch((cause: unknown) => cause)) as ApiError;

    expect(error.status).toBe(502);
    expect(error.detail).toContain("502");
  });
});

// ---------------------------------------------------------------------------
// Readiness is a special case
// ---------------------------------------------------------------------------

describe("readiness", () => {
  it("treats 503 as a successful read of a degraded system", async () => {
    // /readyz returning 503 is the probe *working*. Throwing would blank the health
    // page at exactly the moment it has something to say.
    mockFetch(() => json({ detail: "policy store unreachable" }, 503));

    const result = await api.readiness(SESSION);

    expect(result.ready).toBe(false);
    expect(result.dependencies[0].detail).toContain("policy store");
  });

  it("still propagates a genuine failure", async () => {
    mockFetch(() => json({ detail: "nope" }, 401));

    await expect(api.readiness(SESSION)).rejects.toBeInstanceOf(ApiError);
  });
});

// ---------------------------------------------------------------------------
// The agent is a separate credential on purpose
// ---------------------------------------------------------------------------

describe("agent client", () => {
  it("uses the agent's own URL and key, never the control plane's", async () => {
    let seenUrl = "";
    let seenKey = "";
    mockFetch((url, init) => {
      seenUrl = url;
      seenKey = ((init?.headers ?? {}) as Record<string, string>)["x-api-key"];
      return json({ task: "t", tool_calls: [], side_effects: [] });
    });

    await agentApi.run(SESSION, "do a thing");

    expect(seenUrl).toBe("https://agent.example");
    expect(seenKey).toBe("agent-key");
    expect(seenKey).not.toBe(SESSION.apiKey);
  });

  it("refuses without an agent URL rather than posting to the control plane", async () => {
    await expect(
      agentApi.run({ ...SESSION, agentUrl: "" }, "task"),
    ).rejects.toBeInstanceOf(ApiError);
  });
});
