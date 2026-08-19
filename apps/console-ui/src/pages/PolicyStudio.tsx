/**
 * Policy Studio — read, author, publish, activate, roll back.
 *
 * ## The one design decision that matters here
 *
 * **Publishing and activating are separate buttons, and the console never combines
 * them.** The API supports `POST /v1/policies?activate=true`; this page does not use it.
 *
 * Publishing is safe — it stores an immutable version that governs nobody. Activating is
 * the act that changes what every agent in the tenant is allowed to do, immediately, with
 * no deploy. Collapsing them into one control would make "save my draft" and "change the
 * rules the system enforces" the same gesture, which is precisely the mistake a
 * governance product should not ship.
 *
 * ## Rollback is not a separate thing
 *
 * Activating a lower version *is* the rollback. There is no distinct rollback path
 * anywhere in this system, deliberately: a separate one would be code that runs only
 * during incidents, which is the worst possible test-coverage profile for the operation
 * you most need to work. The server reports `direction` as `rollback`, `rollforward`, or
 * `unchanged`, so the trail identifies it without arithmetic.
 *
 * ## Why an agent can read this page
 *
 * Reading policy is open to any authenticated caller in the tenant. An agent knowing the
 * rules it is bound by is not a risk; an agent editing them is — an agent whose key can
 * rewrite the policy governing it is not governed at all. So the editor renders for
 * everyone and the write controls are gated on `publish_policy`.
 */

import { motion } from "framer-motion";
import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { relativeTime, shortHash } from "@/lib/format";
import { bundleMode, parseBundle, rulesOf, STARTER_BUNDLE, toYaml } from "@/lib/policy";
import { useAsync, useSession } from "@/lib/store";
import type { ValidationResponse } from "@/lib/types";
import { DecisionBadge } from "@/components/DecisionBadge";
import { GlowCard, ShimmerBorder } from "@/components/effects";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorNote,
  Field,
  Input,
  KeyValue,
  SectionTitle,
  Skeleton,
  Textarea,
} from "@/components/ui";

function ValidationPanel({ result }: { result: ValidationResponse }) {
  const accent = result.valid ? "var(--color-allow)" : "var(--color-block)";

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.25 }}
    >
      <Card
        className="p-4"
        // Border colour is a second channel; the word below carries the meaning.
      >
        <div className="flex flex-wrap items-center gap-3">
          <span className="h-2 w-2 rounded-full" style={{ background: accent }} />
          <span
            className="font-mono text-[12.5px] font-semibold tracking-wide"
            style={{ color: accent }}
          >
            {result.valid ? "VALID" : "REJECTED"}
          </span>
          {result.valid && (
            <>
              <Badge>{result.rule_count} rules</Badge>
              <Badge>{result.active_rule_count} active</Badge>
              <Badge tone={result.mode === "shadow" ? "warn" : "neutral"}>
                mode: {result.mode}
              </Badge>
              <Badge>{shortHash(result.content_hash, 8)}</Badge>
            </>
          )}
        </div>

        <p className="mt-2.5 text-[13px] leading-relaxed text-ink-300">{result.detail}</p>

        {result.valid && result.matches_active && (
          <div className="mt-3 rounded-lg border border-[color-mix(in_oklab,var(--color-hitl)_38%,transparent)] bg-[color-mix(in_oklab,var(--color-hitl)_8%,transparent)] p-3">
            <p className="text-[12.5px] leading-relaxed text-hitl">
              This expresses the <strong>same policy already in force</strong>. Compared
              semantically, so key order, omitted defaults, and the version number are not
              differences — publishing this would change nothing.
            </p>
            <p className="mt-1.5 text-[12px] leading-relaxed text-ink-400">
              Worth knowing before you publish: it usually means the file you are editing
              is not the file you think is deployed.
            </p>
          </div>
        )}
      </Card>
    </motion.div>
  );
}

export function PolicyStudioPage() {
  const { session, status, can } = useSession();
  const connected = status === "connected";
  const mayWrite = can("publish_policy");

  const versions = useAsync(
    () => (connected ? api.policies(session) : Promise.resolve(null)),
    [connected, session.baseUrl, session.apiKey],
  );
  const active = useAsync(
    () => (connected ? api.activePolicy(session) : Promise.resolve(null)),
    [connected, session.baseUrl, session.apiKey],
  );

  const [draft, setDraft] = useState("");
  const [loadedFrom, setLoadedFrom] = useState<string>("");
  const [description, setDescription] = useState("");
  const [validation, setValidation] = useState<ValidationResponse | null>(null);
  const [busy, setBusy] = useState<null | "validate" | "publish" | "activate">(null);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Seed the editor from whatever is actually in force, so the first thing an author
  // sees is the truth rather than a template.
  useEffect(() => {
    if (active.data && !draft) {
      setDraft(toYaml(active.data.document));
      setLoadedFrom(`active v${active.data.version}`);
    }
  }, [active.data, draft]);

  const parsed = parseBundle(draft);
  const localRules = rulesOf(parsed.document);

  const loadVersion = async (version: number) => {
    setError(null);
    setValidation(null);
    try {
      const detail = await api.policyVersion(session, version);
      setDraft(toYaml(detail.document));
      setLoadedFrom(`v${version}`);
      setNotice(`Loaded v${version} into the editor. Nothing has changed.`);
    } catch (cause) {
      setError(cause);
    }
  };

  const validate = async () => {
    setBusy("validate");
    setError(null);
    setNotice(null);
    try {
      // Sent as raw YAML: the server parses it with the same loader the deployment uses,
      // so the answer is the deployment's, not this browser's.
      setValidation(await api.validatePolicy(session, { yaml: draft }));
    } catch (cause) {
      setError(cause);
      setValidation(null);
    } finally {
      setBusy(null);
    }
  };

  const publish = async () => {
    setBusy("publish");
    setError(null);
    setNotice(null);
    try {
      const result = await api.publishPolicy(session, { yaml: draft, description });
      setNotice(
        `Published v${result.version}. ${result.detail} Nothing is governed by it until you activate it.`,
      );
      setDescription("");
      versions.reload();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(null);
    }
  };

  const activate = async (version: number) => {
    setBusy("activate");
    setError(null);
    setNotice(null);
    try {
      const result = await api.activatePolicy(session, version);
      setNotice(
        `v${result.active_version} is now in force (${result.direction}, from ` +
          `${result.previous_version ?? "none"}). Warm containers pick this up within the ` +
          `refresh interval; no deploy is involved.`,
      );
      versions.reload();
      active.reload();
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(null);
    }
  };

  if (!connected) {
    return (
      <EmptyState
        icon="§"
        title="Not connected"
        detail="Policy is scoped per tenant — reading another tenant's rules would disclose their controls, which is a map of what they do not defend."
        action={<Button onClick={() => (window.location.hash = "/connect")}>Connect</Button>}
      />
    );
  }

  return (
    <div className="space-y-7">
      <div>
        <h1 className="text-[26px] font-semibold tracking-tight text-ink-100">
          Policy Studio
        </h1>
        <p className="mt-2 max-w-3xl text-[14px] leading-relaxed text-ink-400">
          Every version is immutable and attributed. Publishing stores one; activating puts
          it in force — separately, and on purpose. Activation changes live behaviour within
          the refresh interval with <strong className="text-ink-200">no redeploy</strong>,
          and rolling back is activating a lower number.
        </p>
      </div>

      {!mayWrite && (
        <Card className="border-[color-mix(in_oklab,var(--color-hitl)_35%,transparent)] p-4">
          <p className="text-[13px] leading-relaxed text-ink-300">
            <span className="font-medium text-hitl">Read-only.</span> This key may read
            policy and validate a draft, but not publish or activate. Reading the rules you
            are bound by is not a risk; editing them is — an agent whose key can rewrite
            the policy governing it is not governed at all. Changing policy needs the{" "}
            <span className="font-mono text-ink-100">admin</span> role.
          </p>
        </Card>
      )}

      {notice && (
        <Card className="border-[color-mix(in_oklab,var(--color-allow)_35%,transparent)] p-4">
          <p className="text-[13px] leading-relaxed text-ink-200">{notice}</p>
        </Card>
      )}

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
        {/* ------------------------------------------------------------ editor */}
        <div className="space-y-4">
          <Card className="p-5">
            <SectionTitle
              title="Bundle"
              hint={
                loadedFrom
                  ? `Loaded from ${loadedFrom}. Edits here are local until you publish.`
                  : "Authored as YAML — the form it is reviewed in."
              }
              action={
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      setDraft(STARTER_BUNDLE);
                      setLoadedFrom("a starter template");
                      setValidation(null);
                    }}
                  >
                    Starter
                  </Button>
                  {active.data && (
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => {
                        setDraft(toYaml(active.data!.document));
                        setLoadedFrom(`active v${active.data!.version}`);
                        setValidation(null);
                      }}
                    >
                      Reset to active
                    </Button>
                  )}
                </div>
              }
            />

            <Textarea
              rows={22}
              value={draft}
              onChange={(event) => {
                setDraft(event.target.value);
                setValidation(null);
              }}
              placeholder="apiVersion: guardrail/v1…"
              aria-label="Policy bundle YAML"
            />

            <div className="mt-3 flex flex-wrap items-center gap-3 text-[12px] text-ink-500">
              {parsed.error ? (
                <span className="text-block">{parsed.error}</span>
              ) : (
                <>
                  <span>
                    {localRules.length} rule{localRules.length === 1 ? "" : "s"}
                  </span>
                  <span>·</span>
                  <span>mode {bundleMode(parsed.document)}</span>
                  <span>·</span>
                  <span>parsed locally — the server is the authority</span>
                </>
              )}
            </div>

            <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-ink-800 pt-4">
              <Button
                variant="outline"
                loading={busy === "validate"}
                disabled={busy !== null || !draft.trim()}
                onClick={() => void validate()}
              >
                Validate
              </Button>
              <span className="text-[12px] text-ink-500">
                Changes nothing. Any key may do this.
              </span>
            </div>
          </Card>

          {validation && <ValidationPanel result={validation} />}
          <ErrorNote error={error} />

          {/* -------------------------------------------------------- publish */}
          <Card className="p-5">
            <SectionTitle
              title="Publish"
              hint="Stores an immutable, attributed version. It governs nobody until it is activated — which is a separate, deliberate act."
            />
            {mayWrite ? (
              <>
                <Field
                  label="Why this version exists"
                  hint="Stored with it, so the history explains itself rather than being a list of hashes."
                >
                  <Input
                    value={description}
                    onChange={(event) => setDescription(event.target.value)}
                    placeholder="e.g. raise the bulk-delete threshold to 250 for the migration"
                  />
                </Field>
                <div className="mt-4 flex flex-wrap items-center gap-3">
                  <Button
                    loading={busy === "publish"}
                    disabled={busy !== null || !draft.trim()}
                    onClick={() => void publish()}
                  >
                    Publish as a new version
                  </Button>
                  <span className="text-[12px] text-ink-500">
                    Safe. Nothing changes until you activate it below.
                  </span>
                </div>
                <p className="mt-3 text-[12.5px] leading-relaxed text-ink-500">
                  Consider running{" "}
                  <a className="text-brand-400 underline" href="#/impact">
                    Change Impact
                  </a>{" "}
                  on this draft first — it reports which decisions would differ, before
                  anything is stored.
                </p>
              </>
            ) : (
              <p className="text-[13px] text-ink-500">
                Publishing needs the <span className="font-mono text-ink-300">admin</span>{" "}
                role. The server refuses this with a 403 regardless of what is rendered.
              </p>
            )}
          </Card>
        </div>

        {/* ----------------------------------------------------------- history */}
        <div className="space-y-4">
          <GlowCard className="p-5">
            <ShimmerBorder />
            <SectionTitle title="In force now" />
            {active.loading ? (
              <Skeleton className="h-20 w-full" />
            ) : active.error ? (
              <ErrorNote error={active.error} />
            ) : active.data ? (
              <div className="space-y-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-[20px] font-semibold text-ink-100">
                    v{active.data.version}
                  </span>
                  <Badge tone={active.data.source === "published" ? "good" : "warn"}>
                    {active.data.source}
                  </Badge>
                  <Badge tone={active.data.mode === "shadow" ? "warn" : "neutral"}>
                    {active.data.mode}
                  </Badge>
                </div>
                <KeyValue label="Rules" value={String(active.data.rule_count)} mono />
                {active.data.source === "packaged" && (
                  <p className="text-[12px] leading-relaxed text-ink-500">
                    No stored version is active, so the deployment is running the bundle
                    baked into its build artifact.
                  </p>
                )}
                {active.data.degraded && (
                  <p className="rounded-lg border border-[color-mix(in_oklab,var(--color-hitl)_38%,transparent)] p-2.5 text-[12px] leading-relaxed text-hitl">
                    Degraded — the policy store is unreachable, so the last known good
                    bundle is still being served rather than requests failing.
                  </p>
                )}
              </div>
            ) : null}
          </GlowCard>

          <Card className="p-5">
            <SectionTitle
              title="Version history"
              hint="Append-only by IAM permission — the service role has no DeleteItem."
              action={
                <Button size="sm" variant="ghost" onClick={versions.reload}>
                  Refresh
                </Button>
              }
            />
            {versions.loading ? (
              <Skeleton className="h-32 w-full" />
            ) : versions.error ? (
              <ErrorNote error={versions.error} />
            ) : (versions.data?.versions.length ?? 0) === 0 ? (
              <p className="text-[13px] leading-relaxed text-ink-500">
                Nothing published yet. The deployment is serving the bundle packaged with
                its build — publish one above to take over from it.
              </p>
            ) : (
              <div className="space-y-2">
                {[...(versions.data?.versions ?? [])]
                  .sort((a, b) => b.version - a.version)
                  .map((version) => (
                    <div
                      key={version.version}
                      className={`rounded-lg border p-3 ${
                        version.is_active
                          ? "border-[color-mix(in_oklab,var(--color-allow)_45%,transparent)] bg-ink-850"
                          : "border-ink-800 bg-ink-900/40"
                      }`}
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-[13px] font-semibold text-ink-100">
                          v{version.version}
                        </span>
                        {version.is_active && <Badge tone="good">in force</Badge>}
                        {version.mode === "shadow" && <Badge tone="warn">shadow</Badge>}
                        <span className="ml-auto font-mono text-[11px] text-ink-500">
                          {relativeTime(version.published_at)}
                        </span>
                      </div>
                      <div className="mt-1.5 font-mono text-[11.5px] text-ink-500">
                        {version.rule_count} rules · {shortHash(version.content_hash, 8)}
                      </div>
                      <div className="mt-1 text-[12px] text-ink-400">
                        {version.description || "no description given"}
                      </div>
                      <div className="mt-1 font-mono text-[11px] text-ink-600">
                        by {version.published_by}
                      </div>
                      <div className="mt-2.5 flex flex-wrap gap-2">
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => void loadVersion(version.version)}
                        >
                          Load
                        </Button>
                        {mayWrite && !version.is_active && (
                          <Button
                            size="sm"
                            variant="outline"
                            loading={busy === "activate"}
                            disabled={busy !== null}
                            onClick={() => void activate(version.version)}
                          >
                            {versions.data?.active_version != null &&
                            version.version < versions.data.active_version
                              ? "Roll back to this"
                              : "Activate"}
                          </Button>
                        )}
                      </div>
                    </div>
                  ))}
              </div>
            )}
          </Card>

          <Card className="p-5">
            <SectionTitle title="Rules in the draft" />
            {localRules.length === 0 ? (
              <p className="text-[13px] text-ink-500">
                {parsed.error ?? "No rules in this bundle."}
              </p>
            ) : (
              <div className="space-y-2">
                {localRules.map((rule) => (
                  <div
                    key={rule.id}
                    className="rounded-lg border border-ink-800 bg-ink-900/40 p-2.5"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-[12.5px] text-ink-100">{rule.id}</span>
                      <DecisionBadge effect={rule.effect} size="sm" />
                      {rule.severity && <Badge>{rule.severity}</Badge>}
                      {rule.enabled === false && <Badge tone="warn">disabled</Badge>}
                    </div>
                    {rule.description && (
                      <p className="mt-1.5 text-[12px] leading-relaxed text-ink-400">
                        {rule.description}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            )}
          </Card>
        </div>
      </div>

      {error instanceof ApiError && error.status === 403 && (
        <p className="text-[13px] text-ink-500">
          Refused by the server, not by this page. Policy administration is closed by
          default: <span className="font-mono">GUARDRAIL_POLICY_ADMIN_KEY_IDS</span> defaults
          to empty, meaning nobody, and the `admin` role is the intended grant.
        </p>
      )}
    </div>
  );
}
