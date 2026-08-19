/**
 * Wire types, mirrored by hand from the FastAPI response models.
 *
 * Hand-written rather than generated from `/openapi.json`. A generator would need the
 * live schema at build time, which makes the console un-buildable whenever the API is
 * down — a bad property for the thing you reach for *during* an outage. The surface is
 * small and stable, and `src/lib/api.ts` degrades on unexpected shapes rather than
 * throwing, so a drift shows up as a missing field rather than a blank page.
 */

export type Effect = "allow" | "log_and_allow" | "require_hitl" | "block";

export type DecisionStatus = "pending" | "approved" | "denied" | "expired";

export type Capability =
  | "evaluate"
  | "simulate"
  | "read_audit"
  | "read_decisions"
  | "read_policy"
  | "resolve_decisions"
  | "publish_policy";

export interface Identity {
  key_id: string;
  tenant_id: string;
  name: string;
  role: "agent" | "reviewer" | "admin";
  capabilities: Capability[];
  stage: string;
  version: string;
}

export interface RuleMatch {
  rule_id: string;
  effect: Effect;
  severity: string;
  message: string | null;
}

export interface HitlInstructions {
  decision_id: string;
  timeout_seconds: number;
  on_timeout: "deny" | "allow";
  poll_url: string;
}

export interface EvaluateResponse {
  decision: Effect;
  allowed: boolean;
  matched_rules: RuleMatch[];
  message: string | null;
  decision_id: string;
  audit_seq: number;
  audit_hash: string;
  bundle_id: string;
  bundle_version: number;
  unknown_paths: string[];
  dry_run: boolean;
  hitl: HitlInstructions | null;
  latency_ms: number;
}

export interface SimulateResponse {
  decision: Effect;
  allowed: boolean;
  matched_rules: RuleMatch[];
  message: string | null;
  bundle_id: string;
  bundle_version: number;
  /** `active`, `version`, or `inline` — which bundle answered. */
  bundle_source: string;
  unknown_paths: string[];
  /** The normalised facts the rules were matched against. */
  derived: Record<string, unknown>;
  dry_run: true;
  simulated: true;
  latency_ms: number;
}

export interface AuditEntry {
  seq: number;
  timestamp: string;
  hash: string;
  prev_hash: string;
  decision_id: string;
  tool: string;
  effect: Effect | string;
  matched_rules: Array<Record<string, unknown>>;
  agent_id: string;
  session_id: string;
  dry_run: boolean;
  message: string | null;
  bundle_id: string;
  bundle_version: number;
  arguments: Record<string, unknown>;
  derived: Record<string, unknown>;
  unknown_paths: string[];
  latency_ms: number | null;
}

export interface AuditListResponse {
  entries: AuditEntry[];
  count: number;
  tenant_id: string;
}

export interface VerifyResponse {
  chain_valid: boolean;
  records_checked: number;
  tenant_id: string;
  broken_at_seq: number | null;
  reason: string | null;
}

export interface DecisionView {
  decision_id: string;
  status: DecisionStatus;
  allows_execution: boolean;
  tool: string;
  arguments: Record<string, unknown>;
  agent_id: string;
  session_id: string;
  matched_rules: Array<Record<string, unknown>>;
  message: string | null;
  created_at: string;
  expires_at: number;
  seconds_remaining: number;
  on_timeout: "deny" | "allow";
  reviewers: string[];
  audit_seq: number;
  resolved_at: string | null;
  reviewer: string | null;
  reason: string | null;
}

export interface DecisionQueue {
  decisions: DecisionView[];
  count: number;
  tenant_id: string;
}

export interface DependencyStatus {
  name: string;
  ready: boolean;
  detail: string;
}

export interface ReadinessResponse {
  ready: boolean;
  version: string;
  stage: string;
  dependencies: DependencyStatus[];
}

export interface HealthResponse {
  status: "ok";
  version: string;
  stage: string;
  uptime_seconds: number;
}

/** One tool call the agent attempted, and what policy did about it. */
export interface AgentToolCall {
  tool: string;
  arguments: Record<string, unknown>;
  status: string;
  policy_rules: string[];
  detail: string | null;
  decision_id: string | null;
  audit_seq: number | null;
}

export interface AgentRun {
  task: string;
  session_id: string;
  turns: number;
  duration_ms: number;
  hosted_on: string;
  governed_by: string;
  model: string;
  tool_calls: AgentToolCall[];
  /** What the tools *actually* did. The ledger that makes "blocked" checkable. */
  side_effects: Array<{ tool: string; detail: string }>;
  summary: { executed: number; blocked: number; held_for_review: number };
  final_message: string | null;
}

export interface AgentDescription {
  service: string;
  hosted_on: string;
  governed_by: string;
  llm: {
    provider: string;
    base_url: string;
    model: string;
    api_key_configured: boolean;
  };
}

export interface PolicyVersionSummary {
  version: number;
  published_at: string;
  published_by: string;
  description: string;
  content_hash: string;
  rule_count: number;
  mode: string;
  is_active: boolean;
}

export interface PolicyListResponse {
  bundle_id: string;
  tenant_id: string;
  active_version: number | null;
  /** `published` when a stored version is in force, `packaged` when the deployment is
   * still running the bundle baked into its build artifact. */
  active_source: string;
  degraded: boolean;
  versions: PolicyVersionSummary[];
}

export interface ActiveBundleResponse {
  bundle_id: string;
  version: number;
  source: string;
  degraded: boolean;
  rule_count: number;
  mode: string;
  document: Record<string, unknown>;
}
