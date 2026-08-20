/**
 * The console's behavioural tests.
 *
 * The load-bearing one is `does not render approve or deny for a key that lacks the
 * reviewer role`. The server refuses that with a 403 regardless — this test is about the
 * *console* not claiming an ability the key does not have, which is the exact shape of
 * the defect this system already shipped once: `require_hitl` meant "pause for a human",
 * and in practice meant "pause for anyone holding a key".
 *
 * A second one guards the reverse mistake, which is easier to make and quieter: hiding a
 * control from someone who *does* hold the role. During an incident that is the more
 * expensive failure.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/App";
import { NAV } from "@/components/Shell";
import { EFFECT_MEANING, EFFECT_STYLE, formatClock, shortHash } from "@/lib/format";
import type { Capability, Effect } from "@/lib/types";

const BASE = "https://guardrail.example";

function identity(role: string, capabilities: Capability[]) {
  return {
    key_id: `${role}-1`,
    tenant_id: "acme",
    name: `${role} key`,
    role,
    capabilities,
    stage: "dev",
    version: "abc1234",
  };
}

const HELD_DECISION = {
  decision_id: "dec-1",
  status: "pending",
  allows_execution: false,
  tool: "email.send",
  arguments: { to: ["auditor@external-firm.com"], subject: "Q3" },
  agent_id: "ops-assistant",
  session_id: "session-1",
  matched_rules: [{ rule_id: "external-email-review", effect: "require_hitl" }],
  message: "Human review before any email leaves the organization.",
  created_at: new Date().toISOString(),
  expires_at: Math.floor(Date.now() / 1000) + 900,
  seconds_remaining: 900,
  on_timeout: "deny",
  reviewers: ["security-oncall"],
  audit_seq: 7,
  resolved_at: null,
  reviewer: null,
  reason: null,
};

/** Route by path so one stub serves every screen. */
function stubApi(overrides: Record<string, unknown> = {}) {
  const routes: Record<string, unknown> = {
    "/v1/decisions": { decisions: [HELD_DECISION], count: 1, tenant_id: "acme" },
    "/v1/audit": { entries: [], count: 0, tenant_id: "acme" },
    "/v1/audit/verify": {
      chain_valid: true,
      records_checked: 12,
      tenant_id: "acme",
      broken_at_seq: null,
      reason: null,
    },
    "/v1/policies": {
      bundle_id: "default",
      tenant_id: "acme",
      active_version: 3,
      active_source: "published",
      degraded: false,
      versions: [],
    },
    ...overrides,
  };

  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      const path = url.replace(BASE, "").split("?")[0];
      const body = routes[path];
      if (body === undefined) {
        return Promise.resolve(new Response("{}", { status: 404 }));
      }
      return Promise.resolve(
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    }),
  );
}

/** Put a verified session in place so the app boots straight into a connected state. */
function connectAs(role: string, capabilities: Capability[]) {
  sessionStorage.setItem("gr_base_url", BASE);
  sessionStorage.setItem("gr_api_key", "a-key");
  return identity(role, capabilities);
}

beforeEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
  window.location.hash = "#/";
});

// ---------------------------------------------------------------------------
// Vocabulary completeness
// ---------------------------------------------------------------------------

describe("the four outcomes", () => {
  const EFFECTS: Effect[] = ["allow", "log_and_allow", "require_hitl", "block"];

  it("every outcome has a style and a plain-English meaning", () => {
    // A fifth effect added to the engine must fail here rather than render as a blank
    // badge with no explanation.
    for (const effect of EFFECTS) {
      expect(EFFECT_STYLE[effect]).toBeDefined();
      expect(EFFECT_MEANING[effect].length).toBeGreaterThan(20);
    }
  });

  it("labels every outcome in words, so colour is never the only signal", () => {
    for (const effect of EFFECTS) {
      expect(EFFECT_STYLE[effect].label).toMatch(/[A-Z]/);
    }
    const labels = EFFECTS.map((effect) => EFFECT_STYLE[effect].label);
    expect(new Set(labels).size).toBe(EFFECTS.length);
  });

  it("gives each outcome a distinct colour token", () => {
    const dots = EFFECTS.map((effect) => EFFECT_STYLE[effect].dot);
    expect(new Set(dots).size).toBe(EFFECTS.length);
  });
});

describe("formatters", () => {
  it("abbreviates a hash without implying it is the whole thing", () => {
    const full = "a".repeat(64);
    expect(shortHash(full)).toContain("…");
    expect(shortHash(full).length).toBeLessThan(full.length);
  });

  it("says 'expired' rather than counting into the negatives", () => {
    expect(formatClock(-5)).toBe("expired");
    expect(formatClock(0)).toBe("expired");
    expect(formatClock(125)).toBe("2:05");
  });
});

// ---------------------------------------------------------------------------
// The landing page is readable without a credential
// ---------------------------------------------------------------------------

describe("overview dashboard", () => {
  it("asks for a connection rather than rendering an empty dashboard", async () => {
    // Every tile reads live. Rendering them as zeroes without a key would look exactly
    // like a quiet system, which is the one thing this console must never imply.
    stubApi();

    render(<App />);

    expect(
      await screen.findByText(/reads live from the control plane/i),
    ).toBeInTheDocument();
  });

  it("surfaces a broken chain as a banner, not as one tile among four", async () => {
    // The load-bearing property of the dashboard. A `BROKEN` tile sitting beside three
    // green ones gets scanned past; a banner above them does not.
    stubApi({
      "/v1/me": identity("reviewer", ["read_audit", "resolve_decisions"]),
      "/v1/audit/verify": {
        chain_valid: false,
        records_checked: 812,
        tenant_id: "acme",
        broken_at_seq: 417,
        reason: "content mismatch at seq 417",
      },
    });
    connectAs("reviewer", ["read_audit"]);
    window.location.hash = "#/";

    render(<App />);

    expect(await screen.findByText(/Audit chain broken/i)).toBeInTheDocument();
    expect(screen.getByText(/content mismatch at seq 417/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /investigate/i })).toBeInTheDocument();
  });

  it("shows the pending-review count, and links to the queue when it is not zero", async () => {
    stubApi({ "/v1/me": identity("reviewer", ["read_decisions", "resolve_decisions"]) });
    connectAs("reviewer", ["resolve_decisions"]);
    window.location.hash = "#/";

    render(<App />);

    expect(await screen.findByText(/awaiting review/i)).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: /open the queue/i }),
    ).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Connecting
// ---------------------------------------------------------------------------

describe("connecting", () => {
  it("verifies the key against /v1/me before storing it", async () => {
    stubApi({ "/v1/me": identity("reviewer", ["resolve_decisions"]) });
    window.location.hash = "#/connect";

    render(<App />);

    const user = userEvent.setup();
    await user.type(
      await screen.findByPlaceholderText(/<control-plane>/i),
      BASE,
    );
    await user.type(screen.getByPlaceholderText(/paste your key/i), "a-key");
    await user.click(screen.getByRole("button", { name: /^Connect$/ }));

    await waitFor(() => {
      expect(sessionStorage.getItem("gr_api_key")).toBe("a-key");
    });
  });

  it("stores nothing when the server rejects the key", async () => {
    // The failure this prevents: a console that looks connected while every page it
    // renders is empty for authentication reasons it never mentions.
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ detail: "invalid api key" }), { status: 401 }),
        ),
      ),
    );
    window.location.hash = "#/connect";

    render(<App />);

    const user = userEvent.setup();
    await user.type(await screen.findByPlaceholderText(/<control-plane>/i), BASE);
    await user.type(screen.getByPlaceholderText(/paste your key/i), "wrong");
    await user.click(screen.getByRole("button", { name: /^Connect$/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/401/);
    expect(sessionStorage.getItem("gr_api_key")).toBeNull();
  });

  it("never renders the key back into the page", async () => {
    stubApi({ "/v1/me": identity("reviewer", ["resolve_decisions"]) });
    connectAs("reviewer", ["resolve_decisions"]);
    window.location.hash = "#/connect";

    const { container } = render(<App />);
    await screen.findByText(/connected as/i);

    // The input is a password field, and the key must not appear as visible text
    // anywhere — a screenshot of this console must not leak a credential.
    expect(container.textContent).not.toContain("a-key");
  });
});

// ---------------------------------------------------------------------------
// The permission gate — the reason /v1/me exists
// ---------------------------------------------------------------------------

describe("review queue permissions", () => {
  it("does not render approve or deny for a key that lacks the reviewer role", async () => {
    stubApi({
      "/v1/me": identity("agent", ["evaluate", "read_decisions", "read_audit"]),
    });
    connectAs("agent", ["evaluate", "read_decisions"]);
    window.location.hash = "#/review";

    render(<App />);

    // The held action is visible — reading that your own action is pending is how an
    // agent reports status, and is harmless.
    expect(await screen.findByText("email.send")).toBeInTheDocument();

    // Acting on it is the part that must be refused.
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^deny$/i })).not.toBeInTheDocument();
    expect(screen.getAllByText(/may not resolve held actions|Read-only/i).length).toBeGreaterThan(0);
  });

  it("renders both actions for a reviewer", async () => {
    // Guards the opposite mistake: hiding a control from someone who holds the role.
    // During an incident that is the more expensive failure of the two.
    stubApi({
      "/v1/me": identity("reviewer", [
        "evaluate",
        "read_decisions",
        "resolve_decisions",
      ]),
    });
    connectAs("reviewer", ["resolve_decisions"]);
    window.location.hash = "#/review";

    render(<App />);

    expect(
      await screen.findByRole("button", { name: /approve — let it run/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^deny$/i })).toBeInTheDocument();
  });

  it("shows what is being approved, so approval is not a rubber stamp", async () => {
    stubApi({ "/v1/me": identity("reviewer", ["resolve_decisions"]) });
    connectAs("reviewer", ["resolve_decisions"]);
    window.location.hash = "#/review";

    render(<App />);
    await screen.findByText("email.send");

    expect(screen.getByText(/the full action being approved/i)).toBeInTheDocument();
    expect(screen.getByText(/external-email-review/)).toBeInTheDocument();
  });

  it("shows what happens if nobody answers in time", async () => {
    // The countdown is meaningless without its direction: a decision that expires to
    // `deny` and one that expires to `allow` look identical otherwise, and they are
    // opposites. Asserts the rendered control rather than any wording around it.
    stubApi({ "/v1/me": identity("reviewer", ["resolve_decisions"]) });
    connectAs("reviewer", ["resolve_decisions"]);
    window.location.hash = "#/review";

    render(<App />);
    await screen.findByText("email.send");

    expect(screen.getByText(`\u2192 ${HELD_DECISION.on_timeout}`)).toBeInTheDocument();
  });

  it("surfaces a 403 from the server rather than failing silently", async () => {
    // Belt and braces: even if the capability list were ever wrong, the refusal must
    // reach the operator instead of the click appearing to do nothing.
    const routes: Record<string, unknown> = {
      "/v1/me": identity("reviewer", ["resolve_decisions"]),
      "/v1/decisions": { decisions: [HELD_DECISION], count: 1, tenant_id: "acme" },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        const path = url.replace(BASE, "").split("?")[0];
        if (path.endsWith("/resolve")) {
          return Promise.resolve(
            new Response(JSON.stringify({ detail: "this key may not approve" }), {
              status: 403,
            }),
          );
        }
        return Promise.resolve(
          new Response(JSON.stringify(routes[path] ?? {}), { status: 200 }),
        );
      }),
    );
    connectAs("reviewer", ["resolve_decisions"]);
    window.location.hash = "#/review";

    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /approve — let it run/i }));

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/may not approve/i)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Audit
// ---------------------------------------------------------------------------

describe("audit page", () => {
  it("reports the chain verdict from the server, not from the record list", async () => {
    stubApi({
      "/v1/me": identity("agent", ["read_audit"]),
      "/v1/audit/verify": {
        chain_valid: false,
        records_checked: 40,
        tenant_id: "acme",
        broken_at_seq: 17,
        reason: "content mismatch at seq 17",
      },
    });
    connectAs("agent", ["read_audit"]);
    window.location.hash = "#/audit";

    render(<App />);

    expect(await screen.findByText(/CHAIN BROKEN/)).toBeInTheDocument();
    expect(screen.getByText(/content mismatch at seq 17/)).toBeInTheDocument();
  });

});

// ---------------------------------------------------------------------------
// M7 — the policy surface
// ---------------------------------------------------------------------------

describe("navigation", () => {
  it("every destination renders something other than the fallback", async () => {
    // A route added to the nav but not to the router silently falls through to the
    // Overview, which looks like a working link and is a dead one.
    stubApi({ "/v1/me": identity("admin", ["publish_policy", "resolve_decisions"]) });
    connectAs("admin", ["publish_policy"]);

    for (const item of NAV) {
      if (item.path === "/") continue;
      window.location.hash = `#${item.path}`;
      const view = render(<App />);
      // The Overview's hero is unmistakable; seeing it anywhere else means the route
      // fell through to the default case.
      expect(
        view.queryByRole("heading", { name: /Guardrails for what an agent/i }),
        `${item.path} fell through to the Overview — is it wired in App.tsx?`,
      ).toBeNull();
      view.unmount();
    }
  });
});

describe("policy studio permissions", () => {
  it("hides publish and activate from a key that cannot change policy", async () => {
    // Same shape as the review-queue gate, and the same reasoning: an agent whose key
    // can rewrite the policy governing it is not governed at all.
    stubApi({
      "/v1/me": identity("reviewer", ["read_policy", "resolve_decisions"]),
      "/v1/policies/active": {
        bundle_id: "default",
        version: 3,
        source: "published",
        degraded: false,
        rule_count: 2,
        mode: "enforce",
        document: { apiVersion: "guardrail/v1", rules: [] },
      },
    });
    connectAs("reviewer", ["read_policy", "resolve_decisions"]);
    window.location.hash = "#/policy";

    render(<App />);

    // Reading policy stays open — knowing the rules you are bound by is not a risk.
    expect(await screen.findByText(/Version history/i)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /publish as a new version/i }),
    ).not.toBeInTheDocument();
    expect(screen.getAllByText(/Read-only/i).length).toBeGreaterThan(0);
  });

  it("offers publish to an admin, and keeps it separate from activate", async () => {
    // The load-bearing distinction on that page: publishing is safe, activating is the
    // deliberate act. One button that did both would make "save my draft" and "change
    // what every agent may do" the same gesture.
    stubApi({
      "/v1/me": identity("admin", ["read_policy", "publish_policy"]),
      "/v1/policies/active": {
        bundle_id: "default",
        version: 3,
        source: "published",
        degraded: false,
        rule_count: 2,
        mode: "enforce",
        document: { apiVersion: "guardrail/v1", rules: [] },
      },
      "/v1/policies": {
        bundle_id: "default",
        tenant_id: "acme",
        active_version: 3,
        active_source: "published",
        degraded: false,
        versions: [
          {
            version: 2,
            published_at: new Date().toISOString(),
            published_by: "someone",
            description: "older",
            content_hash: "a".repeat(64),
            rule_count: 2,
            mode: "enforce",
            is_active: false,
          },
        ],
      },
    });
    connectAs("admin", ["read_policy", "publish_policy"]);
    window.location.hash = "#/policy";

    render(<App />);

    expect(
      await screen.findByRole("button", { name: /publish as a new version/i }),
    ).toBeInTheDocument();
    // An older version offers rollback, worded as such rather than as "activate" —
    // the version list loads separately from the active bundle, so wait for it.
    expect(
      await screen.findByRole("button", { name: /roll back to this/i }),
    ).toBeInTheDocument();
  });
});

describe("evidence pages", () => {
  it("tells you how to actually run the proxy", async () => {
    // The proxy is a stdio process and cannot be driven from a browser, so the page's
    // real job is handing over the command. Asserts the command is present rather than
    // any prose about it.
    stubApi({ "/v1/me": identity("agent", ["simulate"]) });
    connectAs("agent", ["simulate"]);
    window.location.hash = "#/mcp";

    render(<App />);

    expect(await screen.findByText(/scripts\/mcp_demo\.py/)).toBeInTheDocument();
    // Appears in the diagram too, so assert the flag that only the command carries.
    expect(screen.getByText(/guardrail-mcp --server/)).toBeInTheDocument();
  });

  it("dry-run states that a parity run writes to the audit chain", async () => {
    // It does, and that is the point — running parity through simulate would prove
    // nothing about the enforcement path. Hiding the cost would be the dishonest part.
    stubApi({
      "/v1/me": identity("agent", ["evaluate"]),
      "/v1/policies/active": {
        bundle_id: "default",
        version: 3,
        source: "published",
        degraded: false,
        rule_count: 2,
        mode: "enforce",
        document: {},
      },
    });
    connectAs("agent", ["evaluate"]);
    window.location.hash = "#/dryrun";

    render(<App />);

    expect(await screen.findByText(/writes to the audit chain/i)).toBeInTheDocument();
  });
});
