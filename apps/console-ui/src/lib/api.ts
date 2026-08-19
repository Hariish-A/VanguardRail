/**
 * The API client, and the session it authenticates with.
 *
 * ## Where the credential lives, and why
 *
 * The reviewer supplies their own key; it is held in `sessionStorage` and sent as
 * `x-api-key`. That is the personal-access-token pattern: the credential belongs to the
 * person using it, is never embedded in the deployed bundle, and dies when the tab
 * closes. It is **not** the production answer — an XSS on this page would read it — and
 * the honest upgrade is Cognito, which needs a user pool, an app client, and a callback
 * domain. That is scoped and deliberately deferred; see `docs/threat-model.md`.
 *
 * What matters is that the key never travels anywhere except the configured base URL,
 * and that the console never widens what the key can do. Authorisation is decided by
 * the server on every single request; `/v1/me` only tells the UI which controls are
 * worth rendering.
 *
 * ## Errors are surfaced, never swallowed
 *
 * `ApiError` carries the status, the server's `detail`, and the `x-request-id`. In a
 * governance product, "something went wrong" is a useless message: the operator needs to
 * know whether the action was refused (403), the key is wrong (401), the tenant has been
 * throttled (429), or the audit write failed (503) — those imply completely different
 * next steps, and the last one means the guardrail is failing *closed*.
 */

import type {
  ActiveBundleResponse,
  AgentDescription,
  AgentRun,
  AuditListResponse,
  DecisionQueue,
  DecisionView,
  Effect,
  EvaluateResponse,
  HealthResponse,
  Identity,
  PolicyListResponse,
  ActivationResponse,
  PublishResponse,
  ReadinessResponse,
  SimulateResponse,
  ValidationResponse,
  VersionDetail,
  VerifyResponse,
} from "./types";

const KEY_BASE_URL = "gr_base_url";
const KEY_API_KEY = "gr_api_key";
const KEY_AGENT_URL = "gr_agent_url";
const KEY_AGENT_KEY = "gr_agent_key";

/**
 * Defaults baked in at build time from `.env`-supplied Vite variables.
 *
 * Only URLs are ever baked in — never a key. A deployed bundle is a public artifact, so
 * embedding a credential would publish it. `VITE_GUARDRAIL_BASE_URL` saves a judge from
 * typing a 60-character Lambda URL; the key still has to be pasted.
 */
export const DEFAULT_BASE_URL: string =
  (import.meta.env.VITE_GUARDRAIL_BASE_URL as string | undefined)?.trim() ?? "";
export const DEFAULT_AGENT_URL: string =
  (import.meta.env.VITE_GUARDRAIL_AGENT_URL as string | undefined)?.trim() ?? "";
export const BUILD_VERSION: string =
  (import.meta.env.VITE_GUARDRAIL_VERSION as string | undefined)?.trim() ?? "local";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
    readonly requestId: string | null,
    readonly path: string,
  ) {
    super(detail);
    this.name = "ApiError";
  }

  /** A sentence an operator can act on, rather than a status code. */
  get guidance(): string {
    switch (this.status) {
      case 0:
        return "The service could not be reached at all. Check the base URL, and that this origin is in the API's allowed CORS origins.";
      case 401:
        return "The API key was not accepted. Check it, or mint a new one with scripts/generate_api_key.py.";
      case 403:
        return "Authenticated, but this key's role does not permit that. Approving needs 'reviewer'; changing policy needs 'admin'.";
      case 404:
        return "Not found for this tenant. Tenancy comes from the key, so another tenant's records are invisible rather than forbidden.";
      case 429:
        return "Rate limited. The per-tenant token bucket refilled slower than you asked; retry after the interval in the response.";
      case 503:
        return "A dependency is unavailable. The guardrail fails closed, so governed agents are being refused rather than allowed through.";
      default:
        return "";
    }
  }
}

export interface Session {
  baseUrl: string;
  apiKey: string;
  agentUrl: string;
  agentKey: string;
}

export function loadSession(): Session {
  return {
    baseUrl: sessionStorage.getItem(KEY_BASE_URL) ?? DEFAULT_BASE_URL,
    apiKey: sessionStorage.getItem(KEY_API_KEY) ?? "",
    agentUrl: sessionStorage.getItem(KEY_AGENT_URL) ?? DEFAULT_AGENT_URL,
    agentKey: sessionStorage.getItem(KEY_AGENT_KEY) ?? "",
  };
}

export function saveSession(session: Session): void {
  sessionStorage.setItem(KEY_BASE_URL, session.baseUrl.replace(/\/+$/, ""));
  sessionStorage.setItem(KEY_API_KEY, session.apiKey);
  sessionStorage.setItem(KEY_AGENT_URL, session.agentUrl.replace(/\/+$/, ""));
  sessionStorage.setItem(KEY_AGENT_KEY, session.agentKey);
}

export function clearSession(): void {
  sessionStorage.clear();
}

async function request<T>(
  session: Session,
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const base = session.baseUrl.replace(/\/+$/, "");
  if (!base) {
    throw new ApiError(0, "No base URL is configured.", null, path);
  }

  let response: Response;
  try {
    response = await fetch(base + path, {
      ...init,
      headers: {
        "content-type": "application/json",
        "x-api-key": session.apiKey,
        ...(init.headers ?? {}),
      },
    });
  } catch (cause) {
    // A CORS rejection and a dead host are indistinguishable from JavaScript — the
    // browser refuses to say which, on purpose. Say so rather than guessing.
    throw new ApiError(
      0,
      `Network request failed: ${cause instanceof Error ? cause.message : String(cause)}`,
      null,
      path,
    );
  }

  const requestId = response.headers.get("x-request-id");
  const text = await response.text();

  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }

  if (!response.ok) {
    const detail =
      (body as { detail?: unknown } | null)?.detail ??
      (body as { error?: unknown } | null)?.error ??
      text.slice(0, 400) ??
      response.statusText;
    throw new ApiError(
      response.status,
      typeof detail === "string" ? detail : JSON.stringify(detail),
      requestId,
      path,
    );
  }

  return body as T;
}

export interface ActionEnvelopeInput {
  agent_id: string;
  session_id: string;
  tool: string;
  arguments: Record<string, unknown>;
  dry_run?: boolean;
  idempotency_key?: string;
}

export const api = {
  me: (s: Session) => request<Identity>(s, "/v1/me"),

  health: (s: Session) => request<HealthResponse>(s, "/healthz"),

  /**
   * `/readyz` answers 503 when a dependency is down — which is a *successful* read of a
   * degraded system, not a failed request. Treating it as an error would make the health
   * page blank at exactly the moment it has something to say.
   */
  readiness: async (s: Session): Promise<ReadinessResponse> => {
    try {
      return await request<ReadinessResponse>(s, "/readyz");
    } catch (error) {
      if (error instanceof ApiError && error.status === 503) {
        return {
          ready: false,
          version: "unknown",
          stage: "unknown",
          dependencies: [
            { name: "readiness", ready: false, detail: error.detail },
          ],
        };
      }
      throw error;
    }
  },

  version: (s: Session) =>
    request<{ version: string; stage: string; service: string }>(s, "/version"),

  evaluate: (s: Session, action: ActionEnvelopeInput) =>
    request<EvaluateResponse>(s, "/v1/evaluate", {
      method: "POST",
      body: JSON.stringify(action),
    }),

  simulate: (s: Session, action: ActionEnvelopeInput) =>
    request<SimulateResponse>(s, "/v1/simulate", {
      method: "POST",
      body: JSON.stringify({ action }),
    }),

  audit: (s: Session, limit = 50, effect?: Effect) =>
    request<AuditListResponse>(
      s,
      `/v1/audit?limit=${limit}${effect ? `&effect=${effect}` : ""}`,
    ),

  verify: (s: Session) => request<VerifyResponse>(s, "/v1/audit/verify"),

  decisions: (s: Session, limit = 50) =>
    request<DecisionQueue>(s, `/v1/decisions?limit=${limit}`),

  decision: (s: Session, id: string) =>
    request<DecisionView>(s, `/v1/decisions/${encodeURIComponent(id)}`),

  resolve: (
    s: Session,
    id: string,
    approve: boolean,
    reason: string,
    reviewer?: string,
  ) =>
    request<DecisionView>(s, `/v1/decisions/${encodeURIComponent(id)}/resolve`, {
      method: "POST",
      body: JSON.stringify({ approve, reason, reviewer: reviewer || undefined }),
    }),

  /**
   * Simulate against a *specific* policy — a published version, or an unpublished
   * candidate supplied inline.
   *
   * This is what makes change-impact analysis possible before anything is stored:
   * reviewing a policy change should not require publishing it first. The server refuses
   * `version` and `bundle` together, so this does too rather than silently preferring one.
   */
  simulateAgainst: (
    s: Session,
    action: ActionEnvelopeInput,
    against: { version?: number; bundle?: Record<string, unknown> } = {},
  ) => {
    if (against.version !== undefined && against.bundle !== undefined) {
      throw new ApiError(
        0,
        "Simulate against a published version or an inline candidate, not both.",
        null,
        "/v1/simulate",
      );
    }
    return request<SimulateResponse>(s, "/v1/simulate", {
      method: "POST",
      body: JSON.stringify({
        action,
        ...(against.version !== undefined ? { version: against.version } : {}),
        ...(against.bundle !== undefined ? { bundle: against.bundle } : {}),
      }),
    });
  },

  policies: (s: Session) => request<PolicyListResponse>(s, "/v1/policies"),

  activePolicy: (s: Session) =>
    request<ActiveBundleResponse>(s, "/v1/policies/active"),

  policyVersion: (s: Session, version: number) =>
    request<VersionDetail>(s, `/v1/policies/versions/${version}`),

  /** Lint a bundle without storing it. Open to any authenticated caller — knowing
   *  whether a draft parses is not a privileged operation. */
  validatePolicy: (s: Session, body: { bundle?: unknown; yaml?: string }) =>
    request<ValidationResponse>(s, "/v1/policies/validate", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /**
   * Publish a new immutable version. Requires the `admin` role.
   *
   * `activate` defaults to false, and the console never sets it on the publish call —
   * publishing is safe, activating is the deliberate act, and collapsing them into one
   * button would make "save my draft" and "change what every agent is allowed to do"
   * the same gesture.
   */
  publishPolicy: (
    s: Session,
    body: { bundle?: unknown; yaml?: string; description?: string },
  ) =>
    request<PublishResponse>(s, "/v1/policies", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Make a version the one in force. Rolling back is this with a lower number —
   *  there is deliberately no separate rollback path. */
  activatePolicy: (s: Session, version: number) =>
    request<ActivationResponse>(s, `/v1/policies/versions/${version}/activate`, {
      method: "POST",
    }),
};

/**
 * The AWS-hosted demo agent. A different origin and a different key from the control
 * plane, deliberately: the agent is an ordinary third-party SDK consumer, and giving the
 * console one credential for both would blur exactly the boundary this project is about.
 */
export const agentApi = {
  describe: async (s: Session): Promise<AgentDescription> => {
    const response = await fetch(s.agentUrl.replace(/\/+$/, ""), { method: "GET" });
    if (!response.ok) {
      throw new ApiError(response.status, await response.text(), null, "agent");
    }
    return (await response.json()) as AgentDescription;
  },

  run: async (
    s: Session,
    task: string,
    options: { dryRun?: boolean; maxTurns?: number } = {},
  ): Promise<AgentRun> => {
    if (!s.agentUrl) {
      throw new ApiError(0, "No agent URL is configured.", null, "agent");
    }
    let response: Response;
    try {
      response = await fetch(s.agentUrl.replace(/\/+$/, ""), {
        method: "POST",
        headers: { "content-type": "application/json", "x-api-key": s.agentKey },
        body: JSON.stringify({
          task,
          dry_run: options.dryRun ?? false,
          max_turns: options.maxTurns ?? 4,
        }),
      });
    } catch (cause) {
      throw new ApiError(
        0,
        `Could not reach the agent: ${cause instanceof Error ? cause.message : String(cause)}`,
        null,
        "agent",
      );
    }

    const text = await response.text();
    let body: unknown = null;
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }

    if (!response.ok) {
      const detail =
        (body as { detail?: string } | null)?.detail ?? text.slice(0, 400);
      throw new ApiError(response.status, detail, null, "agent");
    }
    return body as AgentRun;
  },
};
