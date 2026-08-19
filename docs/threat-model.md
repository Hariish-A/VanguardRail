# Threat model

A guardrail is a security control, so the honest question is not "what does it stop?" but
"how would someone get past it?" This document answers the second, including the cases
where the answer is uncomfortable.

Scope: the deployed control plane, the SDK, the MCP proxy, and the policy lifecycle.

---

## What this system actually protects

**The asset is the *action*** — a database delete, an outbound email, a file read — and the
property being defended is that no governed action executes without being evaluated and
recorded first.

Two things follow, and both are limits rather than features:

* It governs actions that **pass through it**. An agent with a direct database credential
  is not governed by anything here, exactly as a firewall does not inspect a cable that
  bypasses it.
* It is **tamper-evident, not tamper-proof**. Someone with sufficient IAM permission can
  delete the audit table. What they cannot do is quietly alter one record and leave the
  log internally consistent, which is the realistic insider scenario.

---

## Trust boundaries

```
   [ agent process ]      untrusted: writes the tool call, chooses the arguments
          │  HTTPS + x-api-key
          ▼
   [ Function URL ]       public by design; authentication is the control
          │
   [ FastAPI app ]        trusted: evaluates, decides, records
          │  IAM role (PutItem/GetItem/Query/UpdateItem — never DeleteItem)
          ▼
   [ DynamoDB ]           trusted store; hash chain makes edits detectable

   [ reviewer ]           semi-trusted: can approve, cannot rewrite policy
   [ policy admin ]       highest privilege in the system
```

---

## Adversaries, and what each can do

### 1. A compromised or misbehaving agent

**Capability:** full control over the tool name and arguments it submits, and over how
often it submits them.

| Attempt | Outcome | Mechanism |
|---|---|---|
| Craft arguments to dodge a threshold | Blocked | Derived facts normalise `count`, id lists, and `WHERE` clauses into one `record_count` |
| `bcc` an outsider instead of `to` | Held for review | The recipient extractor reads `to`, `cc`, and `bcc` |
| Use a Windows path to dodge a `/` rule | Blocked | The path extractor normalises separators before any rule sees it |
| Send an unparseable value so no rule matches | Blocked/held | `UNKNOWN` resolves toward the restrictive outcome, never the permissive one |
| Claim another tenant in the request body | Ignored | Tenant comes from the verified API key, never the body |
| Publish a policy that permits its own next action | **403** | Policy writes need `require_policy_admin`, a separate permission |
| Flood the endpoint | 429, then throttled | Per-tenant token bucket; account concurrency quota as the hard ceiling |
| Retry to get a second bite | Same decision returned | Idempotency key returns the original decision |

**Residual risk — stated plainly.** An agent that *does not call the SDK at all* is not
governed. Enforcement lives at the call site, so the guarantee is only as strong as the
deployment discipline that routes tool calls through it. The MCP proxy is the strongest
answer available here, because it runs the upstream server as its own child process and is
therefore the only path to it.

### 2. A leaked agent API key

**Capability:** everything the agent could do, from anywhere.

Contained by: the key is scoped to one tenant; it cannot publish policy; it cannot resolve
held decisions belonging to others; every action it takes is recorded against its
`key_id`; the per-tenant rate limit bounds the damage rate.

**Residual risk:** keys live in an environment variable and rotation requires a redeploy.
There is no revocation list. A DynamoDB-backed key store was scoped for M5 and **not
built** — the free-tier write capacity is already committed to the audit chain, and adding
a key lookup to the hot path would have cost a read per request. This is a real gap, not
an oversight.

### 3. A malicious or careless policy administrator

**Capability:** the highest privilege in the system — they can publish a bundle that
permits anything.

Contained by: publishing is inert until activation; every version is immutable and
attributed (`published_by`, `activated_by`, timestamps); the history is append-only *by
IAM permission*, since the role has no `DeleteItem`; a rollback is itself a recorded
activation.

**Residual risk:** there is no second-person approval on policy activation, and bundles are
not cryptographically signed. A determined administrator can weaken policy and the trail
will show exactly who did it — detection, not prevention.

### 4. An insider with AWS console access

**Capability:** direct access to DynamoDB and CloudFormation.

| Attempt | Detected? |
|---|---|
| Edit one audit record | **Yes** — the recomputed hash no longer matches |
| Delete a record from the middle | **Yes** — a sequence gap |
| Reorder records | **Yes** — a broken `prev_hash` link |
| Delete the whole table | **No** — but point-in-time recovery is enabled |
| Rewrite the entire chain consistently | **No** — see below |

**Residual risk — the honest one.** The chain is self-referential: an attacker who can
write to the table can recompute every hash from a forged record onward and produce an
internally consistent history. Detecting *that* requires anchoring the chain somewhere the
attacker does not control — publishing the head hash to an append-only store, another
account, or a third party. **Not implemented.** The chain defends against selective
tampering, which is the common case, not against a full rewrite by someone with table-wide
write access.

### 5. A network attacker

The Function URL is public by deliberate design (CloudFront was removed — a new AWS
account cannot create distributions without a support case). TLS is enforced by AWS.
Every non-health route rejects unauthenticated callers, asserted on every deploy by CI and
by `test_every_data_endpoint_rejects_unauthenticated_requests`.

**Residual risk:** the endpoint is reachable and therefore enumerable. Authentication is
the only barrier, and there is no WAF (AWS WAF costs $5/month per web ACL).

### 6. A hostile policy file

Policy is untrusted input — it may arrive over the API.

* No `eval`, no `exec`, and a **closed operator table**. An unknown operator is a load-time
  error, never a silently skipped predicate.
* `yaml.safe_load` only. Full YAML can construct arbitrary Python objects, which is the
  same class of hole as `eval`.
* Message interpolation is hand-rolled, not `str.format` — `str.format` exposes attribute
  and index access on arbitrary objects.
* Regexes are compiled at load time so an invalid one fails on upload, not mid-evaluation.
* A rule naming a nonexistent derived fact is rejected, because a rule that can never
  match looks exactly like coverage.

**Residual risk:** a catastrophically backtracking regex could be published by a policy
admin and slow evaluation. There is no regex complexity limit.

### 7. Denial of service

The most interesting case, because the correct behaviour looks like an outage.

At sustained load the audit table hits its 5 provisioned WCU ceiling and writes throttle.
The service then returns **503 with `Retry-After`** rather than a decision, and a
fail-closed client blocks the action. **An attacker who can saturate the guardrail can
therefore stop governed agents from acting** — availability is traded for safety,
deliberately, because permitting an unrecorded action is the exact gap this system exists
to close.

That trade-off is the right one for a governance control, but it should be understood
before deployment, and it is the reason the rate limiter exists.

---

## Deliberate design decisions with security consequences

| Decision | Gains | Costs |
|---|---|---|
| Fail closed by default | An outage cannot become a bypass | An outage becomes an agent stoppage |
| Public Function URL, no CloudFront | Works on an unverified account | No edge filtering; auth is the only barrier |
| In-process rate limiting | Costs no DynamoDB capacity | Per-container, so the real bound is `containers x rate` |
| Policy store outage serves the last known good bundle | A store outage is not an agent outage | A cold start during an outage falls back to the *packaged* bundle, which may be more permissive — logged, and surfaced by `/readyz` |
| Simulation writes no audit record | Speculation does not dilute evidence | `/v1/simulate` leaves no trace, so policy exploration is not in the chain (it is still logged) |
| No `DeleteItem` in the IAM role | The service cannot erase its own evidence | Expired decisions rely on DynamoDB TTL |

---

## Known gaps, ranked

1. **No external anchoring of the audit chain.** A table-wide writer can rewrite history
   consistently.
2. **API keys cannot be revoked without a redeploy.** No key store, no revocation list.
3. **No second-person approval on policy activation**, and bundles are unsigned.
4. **No WAF or IP reputation filtering** in front of the public endpoint.
5. **Enforcement depends on the call path.** An agent that bypasses the SDK and the proxy
   is ungoverned.
6. **Console authentication is an API key in `sessionStorage`.** Cognito was scoped and
   deferred; this is acceptable for a single-reviewer demo and not for production.

Items 1–3 are the ones that would matter first in a real deployment.
