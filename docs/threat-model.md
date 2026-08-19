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

   [ browser ]            runs the console; holds the reviewer's key in sessionStorage
          │  HTTPS
          ▼
   [ S3 bucket ]          public by design: it serves a compiled frontend and nothing
                          else. Read-only to the world; public ACLs blocked, so the
                          single grant path is one reviewed bucket policy
```

Authorisation is by **role** on the API key: `agent` < `reviewer` < `admin`, defaulting to
`agent`. A key that states no role gets the least privilege, never the most.

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
| Publish a policy that permits its own next action | **403** | Policy writes need the `admin` role |
| **Approve the action its own policy held for review** | **403** | Resolving needs the `reviewer` role. See below — this was a real defect |
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
held decisions **at all** unless it holds the `reviewer` role; every action it takes is recorded against its
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

### 6. An attacker targeting the console

New in M6. The console is a static bundle on a world-readable S3 bucket, and it takes an
API key from the person using it.

| Attempt | Outcome |
|---|---|
| Read the bundle for a credential | Nothing to find. Only URLs are baked in, and CI fails the build if a key-shaped string appears in `dist/` |
| Replace the console with a page that harvests keys | Requires write access to the bucket. The public statement grants read only, asserted by `test_the_public_policy_grants_read_only`; writing needs IAM |
| Upload an object with a permissive ACL | Blocked. `BlockPublicAcls` and `IgnorePublicAcls` stay on, so object ACLs cannot widen access beyond the bucket policy |
| Call the API from a page they control | Refused at preflight. Origins are an explicit allowlist, never `*` — a wildcard with credentials is rejected by browsers and wrong in principle |
| Persuade the console to show a control the key lacks | The console renders only what `/v1/me` reports, and the server enforces independently on every request. A capability drift is caught by a test that tries every verb |

**Residual risk — the real one.** The key lives in `sessionStorage`, so **an XSS on this
page reads it**. The bundle has no third-party scripts and no CDN, the CSP surface is one
origin, and the credential dies with the tab — but that is mitigation, not elimination.
Cognito hosted sign-in removes the problem properly and is deferred; it needs a user pool,
an app client, and a callback domain, which buys little while there is one reviewer.

A second, smaller one: the S3 **website** endpoint is HTTP only, because S3 website
hosting cannot terminate TLS. A page served over plain HTTP is tamperable in transit, and
this page takes a key. Both endpoints are emitted and the HTTPS REST endpoint is the one
documented and handed out — but the HTTP one exists and works.

### 7. A hostile policy file

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

### 8. Denial of service

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

## A defect this model previously missed

Worth recording, because the threat model itself asserted a protection that did not exist.

`/v1/decisions/{id}/resolve` required only a **valid API key** — not a particular one. So
any key could approve any of its own tenant's held actions, **including the agent's own
key**. `require_hitl` means "pause for a human"; in practice it meant "pause for anyone
holding a key", and the agent being paused held one.

Verified against the live deployment: the AWS-hosted agent had an external email held by
`external-email-review`, then approved it with its own key. The audit chain recorded
`reviewer: the-agent-itself`.

The identical argument had already been written down one section over, for policy
administration — *"an agent whose key can rewrite the policy governing it is not
governed"* — and simply was never extended to approval. Reasoning about one privilege
correctly does not generalise on its own.

Closed by the role model. Re-running the same attack now returns **403** with the decision
still `pending` and `allows_execution: false`.

## Known gaps, ranked

1. **No external anchoring of the audit chain.** A table-wide writer can rewrite history
   consistently.
2. **API keys cannot be revoked without a redeploy.** No key store, no revocation list.
   Roles are carried on the key table, so changing a role is also a redeploy.
3. **No second-person approval on policy activation**, and bundles are unsigned.
4. **No WAF or IP reputation filtering** in front of the public endpoint.
5. **Enforcement depends on the call path.** An agent that bypasses the SDK and the proxy
   is ungoverned.
6. **Console authentication is an API key in `sessionStorage`.** Cognito was scoped and
   deferred; this is acceptable for a single-reviewer demo and not for production. An XSS
   on the console page would disclose the reviewer's key.
7. **The console bucket is publicly readable and served over HTTP as well as HTTPS.**
   Read-only, holding only a compiled frontend — but the HTTP website endpoint is
   tamperable in transit, and the page takes a credential.

Items 1–3 are the ones that would matter first in a real deployment.
