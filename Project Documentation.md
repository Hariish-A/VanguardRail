# Guardrail — Project Documentation

Action-layer governance for AI agents. This document explains the *concepts*, the
*models*, the *pipeline*, and *how each part works*. For build status and decisions
made, see `PROGRESS.md`.

---

## Table of contents

1. [The problem](#1-the-problem)
2. [Core concepts](#2-core-concepts)
3. [The policy model](#3-the-policy-model)
4. [The pipeline, traced end to end](#4-the-pipeline-traced-end-to-end)
5. [The four outcomes](#5-the-four-outcomes)
6. [Human-in-the-loop state machine](#6-human-in-the-loop-state-machine)
7. [The audit log and its hash chain](#7-the-audit-log-and-its-hash-chain)
8. [Models and LLM usage](#8-models-and-llm-usage)
9. [AWS architecture](#9-aws-architecture)
10. [The policy lifecycle](#10-the-policy-lifecycle)
11. [Simulation, dry run, and evidence](#11-simulation-dry-run-and-evidence)
12. [Integrating with existing agents](#12-integrating-with-existing-agents)
13. [Hardening: limits, failure, and cost](#13-hardening-limits-failure-and-cost)
14. [Repository layout](#14-repository-layout)
15. [Running it](#15-running-it)
16. [The review console](#16-the-review-console)
17. [Roadmap](#17-roadmap)

---

## 1. The problem

### What existing guardrails do

Every commercial guardrails platform — and every open-source equivalent — operates on
**text**. They sit on two edges of the model:

```
user input ──► [ input guardrail ] ──► LLM ──► [ output guardrail ] ──► user
```

They check for toxicity, prompt injection, PII leakage, jailbreaks, off-topic drift.
All of that is worth doing.

### What none of them do

An agent is not a chatbot. After the model produces text, an **agent loop** parses that
text into **tool calls** and executes them against real systems:

```
LLM output ──► parse tool_calls ──► execute ──► database / email / filesystem / API
                                        ▲
                                        └── nothing is watching here
```

The gap is precise and consequential: **a perfectly clean, non-toxic, non-injected model
response can still instruct a tool to delete ten thousand database records.** A text
guardrail inspects the sentence "I'll clean up the inactive accounts now" and finds
nothing wrong with it. It has no opinion about the `DELETE` affecting 10,000 rows that
follows.

Policy documents do not close this gap either. A policy that says "bulk deletions require
approval" is reviewed quarterly by humans; the agent acts in 200 milliseconds. This is the
**enforcement gap** — the distance between policy as written and policy as applied at the
moment of execution.

### What Guardrail does

Guardrail evaluates the **action**, not the text, and it does so **before dispatch**:

```
LLM output ──► parse tool_calls ──► [ GUARDRAIL ] ──► execute (or not)
                                          │
                                          ├─ allow          → execute, audit it
                                          ├─ log_and_allow  → execute, flag for review
                                          ├─ require_hitl   → pause, wait for a human
                                          └─ block          → never dispatched
```

The critical property is that evaluation happens **pre-execution**. Detecting a bulk
delete after it has run is forensics. Guardrail is a control.

---

## 2. Core concepts

### 2.1 Action envelope

The unit Guardrail reasons about. An agent's intent to call one tool, normalized into a
structure the policy engine can evaluate:

```jsonc
{
  "tenant_id": "acme",
  "agent_id": "support-bot-v3",
  "session_id": "sess_01H...",
  "tool": "db.delete_records",
  "arguments": { "table": "users", "where": "last_login < '2023-01-01'" },
  "principal": { "type": "agent", "on_behalf_of": "user_8842" },
  "context": { "environment": "production" },
  "dry_run": false
}
```

Everything the engine needs travels in this envelope. The engine never reaches out to
fetch more — that is what keeps evaluation fast, deterministic, and reproducible.

### 2.2 Derived facts (extractors)

Policies should not be written against raw arguments, because the same intent arrives in
many shapes. "How many records does this delete affect?" might come from an explicit
`count` argument, a list of ids, or a SQL `WHERE` clause.

An **extractor** normalizes these into a derived fact:

```
arguments.where = "last_login < '2023-01-01'"
        │
        ▼  record_count extractor
derived.record_count = 8412
```

A policy author then writes **one** rule against `derived.record_count` instead of five
rules against five argument shapes.

**When an extractor cannot determine a value it returns `UNKNOWN`, and `UNKNOWN` on a
`block` rule fails closed.** If we cannot tell how many records a delete affects, we do
not assume it is few.

### 2.3 Policy bundle

A versioned, declarative collection of rules (YAML). Bundles are stored versioned and
activated by pointer, so policy can change without redeploying code, and a bad policy can
be rolled back in seconds.

### 2.4 Effect and resolution

Four outcomes, **ordered by restrictiveness**. When several rules match one action, the
**most restrictive wins**:

```
ALLOW (0)  <  LOG_AND_ALLOW (1)  <  REQUIRE_HITL (2)  <  BLOCK (3)
```

This is implemented as an `IntEnum` so resolution is literally `max(matched_effects)`
(`packages/guardrail-core/src/guardrail_core/effects.py`).

**Why not first-match-wins?** Because under first-match, *reordering a policy file
silently weakens security*. Someone moves a broad `allow` rule above a narrow `block` rule
during a tidy-up, and the block stops firing with no error and no diff that looks
dangerous. Most-restrictive-wins is order-independent by construction.

### 2.5 Fail-closed

If Guardrail is unreachable, the SDK **blocks by default** rather than allowing. A
governance control that disappears under load is not a control. Fail-open is available as
an explicit, logged configuration choice — never a silent default.

---

## 3. The policy model

### 3.1 Bundle structure

```yaml
apiVersion: guardrail/v1
metadata:
  bundle_id: default
  version: 3
  mode: enforce            # enforce | shadow  (shadow = evaluate but never act)
defaults:
  effect: allow            # applied when no rule matches
  resolution: most_restrictive
rules:
  - id: db-bulk-delete
    description: Block destructive deletes above the blast-radius threshold
    severity: critical
    match:
      tool: "db.delete_records"
      all:
        - { path: "derived.record_count", op: gt, value: 100 }
    effect: block
    message: "Bulk delete of {derived.record_count} records exceeds the limit of 100."
```

### 3.2 Rule fields

| Field | Meaning |
|---|---|
| `id` | Stable identifier. Appears in every audit record this rule matches. |
| `description` | Human-readable intent. Shown to reviewers in the HITL console. |
| `severity` | `low` / `medium` / `high` / `critical`. Drives console sorting and alerting. |
| `match.tool` | Tool name or glob (`db.*`). |
| `match.all` | Every predicate must hold (logical AND). |
| `match.any` | At least one predicate must hold (logical OR). |
| `effect` | `block` / `require_hitl` / `log_and_allow` / `allow`. |
| `message` | Explanation returned to the agent. Interpolates `{derived.*}` and `{args.*}`. |
| `hitl` | For `require_hitl`: `timeout_seconds`, `on_timeout`, `reviewers`. |

### 3.3 Predicate operators

A **fixed** operator set: `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`, `not_in`, `any_in`,
`any_not_in`, `contains`, `icontains`, `matches` (regex), `glob`, `exists`.

Evaluated by a hand-written AST walker — **never `eval()` or `exec()`**.

This is not incidental. Policy files are *untrusted input*: they arrive from a git repo, an
API call, or an S3 object. Evaluating them as code would hand arbitrary execution to
anyone who can edit a policy — in the one service whose entire job is preventing
unauthorized actions. A closed operator set is also auditable: a reviewer can enumerate
everything a rule is capable of doing.

### 3.4 The three worked examples from the problem statement

```yaml
# 1. Block a bulk delete
- id: db-bulk-delete
  match:
    tool: "db.delete_records"
    all: [{ path: "derived.record_count", op: gt, value: 100 }]
  effect: block

# 2. Human review for external email
- id: external-email-review
  match:
    tool: "email.send"
    any: [{ path: "derived.recipient_domains", op: any_not_in, value: ["acme-corp.com"] }]
  effect: require_hitl
  hitl: { timeout_seconds: 900, on_timeout: deny }

# 3. Permit but record confidential reads
- id: confidential-read-audit
  match:
    tool: "file.read"
    all: [{ path: "args.path", op: icontains, value: "confidential" }]
  effect: log_and_allow
```

---

## 4. The pipeline, traced end to end

What happens when an agent tries to delete 500 records.

### Step 0 — The agent decides

Qwen3 (via Ollama) receives a task and emits an OpenAI-style tool call:

```json
{ "name": "db.delete_records", "arguments": { "table": "users", "count": 500 } }
```

At this instant nothing has happened yet. This is the moment Guardrail exists to intercept.

### Step 1 — The SDK intercepts

The tool is wrapped by `@governed_tool`. The decorator runs **before** the function body:

```python
@governed_tool(name="db.delete_records")
def delete_records(table: str, count: int) -> int:
    return db.execute(...)  # never reached if the decision is `block`
```

The SDK builds the action envelope and POSTs it to `/v1/evaluate`.

### Step 2 — Edge

```
SDK ──HTTPS──► Lambda Function URL ──► Lambda
```

**As deployed there is no CloudFront.** A new AWS account cannot create a distribution
without a support case, and the project could not wait on a support queue. So the Function
URL is the edge, with `authType: NONE`, and **authentication is the control** — a hashed
API key compared in constant time inside the application, on every non-health route.

That is a weaker posture than the designed one, and it is worth being precise about how.
Hiding the origin was defence in depth; it was never the actual control. What is lost is
edge filtering and the ability to reject an unauthenticated request before it reaches
compute. What is kept — and is testable, which the CloudFront version was not — is that
every data route rejects an unauthenticated caller, asserted on every deploy by
`test_every_data_endpoint_rejects_unauthenticated_requests`.

CloudFront remains wired and optional: `GUARDRAIL_ENABLE_CLOUDFRONT=true` switches the
Function URL to `AWS_IAM` and puts an Origin Access Control in front of it, at which point
the raw URL returns 403 to everyone but the distribution.

Browser callers get one more thing here: the Function URL answers CORS preflight itself,
so the `OPTIONS` round trip never pays a Lambda cold start. The allowlist is explicit
origins, never `*` — these requests carry credentials, and a wildcard origin with
credentials is both rejected by browsers and wrong in principle.

### Step 3 — Request context

Mangum converts the event to ASGI. Middleware assigns a `request_id` (honouring a
caller-supplied one so a trace spans agent → SDK → service) and starts the latency timer.

### Step 4 — Extraction

Extractors run over the envelope, producing `derived.record_count = 500`.

### Step 5 — Evaluation

The active bundle is compiled once per warm Lambda container and cached by version. Every
rule is evaluated — not just until the first match:

```
db-bulk-delete           → matches → BLOCK
confidential-read-audit  → no match
external-email-review    → no match

resolution = max([BLOCK]) = BLOCK
```

**No network calls, no database reads, no LLM.** Pure computation on the envelope, which
is what keeps p99 low and the decision reproducible.

### Step 6 — Audit

A record is written to DynamoDB containing the envelope, the decision, **every** matched
rule id, the bundle version, latency, and a hash chained to the previous record.

### Step 7 — Response

```json
{
  "decision": "block",
  "matched_rules": ["db-bulk-delete"],
  "message": "Bulk delete of 500 records exceeds the limit of 100.",
  "audit_id": "01H...",
  "request_id": "..."
}
```

### Step 8 — Enforcement

The SDK raises `ActionBlocked`. **The function body never runs.** The SDK returns a
structured refusal to the model, so the agent can explain the denial to the user rather
than crashing:

> "I wasn't able to delete those records — policy `db-bulk-delete` blocks deletions over
> 100 rows. Would you like me to request approval, or narrow the selection?"

### The contrast

For a 5-record delete, steps 0–4 are identical; step 5 finds no matching rule, resolution
falls to the bundle default `allow`, and step 8 executes the function normally. **The
audit record is written either way** — allows are evidence too.

---

## 5. The four outcomes

| Outcome | Tool runs? | Audit record | Agent sees | Typical use |
|---|---|---|---|---|
| `allow` | Yes | Yes | Normal return value | Routine, in-policy actions |
| `log_and_allow` | Yes | Yes, flagged | Normal return value | Sensitive but permitted — confidential reads |
| `require_hitl` | **Only after approval** | Yes, plus the decision record | Blocks until resolved, or a pending handle | Irreversible or outward-facing actions |
| `block` | **No** | Yes | Structured refusal | Destructive or forbidden actions |

`log_and_allow` matters more than it first appears: it is how an organization gets
visibility without friction. Blocking every confidential-file read would make the agent
useless; having no record of them is unacceptable in an audit. This is the middle path,
and it is why "guardrail" is not a synonym for "blocklist".

---

## 6. Human-in-the-loop state machine

```
                    ┌─────────┐
   evaluate ───────►│ PENDING │
   → require_hitl   └────┬────┘
                         │
        ┌────────────────┼────────────────┐
        │                │                │
   reviewer         reviewer          TTL expires
   approves           denies                │
        │                │                  │
        ▼                ▼                  ▼
  ┌──────────┐     ┌──────────┐      ┌─────────────┐
  │ APPROVED │     │  DENIED  │      │   EXPIRED   │
  └────┬─────┘     └────┬─────┘      └──────┬──────┘
       │                │                   │
       ▼                ▼                   ▼
  tool executes    refusal to agent    on_timeout (default: deny)
```

**Design points:**

- **Conditional writes.** Resolution uses a DynamoDB conditional update on
  `status = PENDING`, so two reviewers clicking simultaneously produce exactly one winner;
  the loser receives a clean `409` rather than silently overwriting.
- **Timeout defaults to deny.** An approval request nobody answers must not become an
  approval. `on_timeout` is configurable per rule but defaults to the safe direction.
- **Reviewer identity and reason are recorded** and linked into the audit chain, so the
  log answers "who approved this, and what did they say" — not merely "it was approved".
- **Two SDK modes.** Blocking wait with bounded exponential backoff for synchronous
  agents; an async pending handle for long-running ones.

---

## 7. The audit log and its hash chain

Every evaluated action produces a record — allows included. An audit log that only records
denials cannot answer "what did this agent do last Tuesday", which is the question
compliance actually asks.

### Tamper evidence

Each record carries the hash of its predecessor:

```
record[n].hash = sha256( record[n-1].hash || canonical_json(record[n]) )
```

Altering or deleting record *n* breaks every hash from *n* onward, and `GET /v1/audit/verify`
walks the chain to detect it.

This is deliberately **tamper-evident, not tamper-proof**. Someone with sufficient IAM
permissions can still delete the table. What they cannot do is quietly change one record
and leave the log looking consistent — which is the realistic insider scenario.

### What each record contains

The action envelope; the decision; **all** matched rule ids; the policy bundle version
(so a past decision can be reproduced against the policy in force at the time);
evaluation latency; the principal; the `dry_run` flag; the `request_id`; and the hashes.

---

## 8. Models and LLM usage

### 8.1 The decision path never calls an LLM

This is worth stating plainly because it is counterintuitive for an "AI governance"
system. Policy evaluation is **deterministic**: rules, operators, extractors. No inference.

Three consequences:

1. **Speed.** No inference latency in the hot path of every tool call.
2. **Reproducibility.** The same envelope and bundle always yield the same decision — a
   requirement for anything used as evidence.
3. **No rate-limit exposure.** A free LLM tier can never throttle enforcement.

### 8.2 Where the LLM is used

| Use | Component | Model |
|---|---|---|
| Generating tool calls (the workload being governed) | demo agent, M2 | Ollama `qwen3:4b` |
| Optional semantic rules (`llm_judge`) | policy engine, M4+ | same, off the hot path, fail-closed on timeout |

### 8.3 Why Qwen3 on Ollama

- **Free and open-weight.** No API key, no billing, no credit card.
- **Native tool calling on every size** (0.6B → 32B) with no custom Modelfile — essential,
  since this project exists to intercept tool calls.
- **No rate limits**, unlike any hosted free tier.
- `qwen3:4b` is 2.5 GB with a 256K context window; `qwen3:1.7b` (1.4 GB) also does tool
  calling if RAM is tight.

### 8.4 The provider abstraction

Ollama exposes an **OpenAI-compatible** `/v1/chat/completions` endpoint. The SDK therefore
targets that wire format, and provider, model, and base URL are configuration:

```
GUARDRAIL_LLM_BASE_URL=http://localhost:11434/v1
GUARDRAIL_LLM_MODEL=qwen3:4b
```

Groq, OpenAI, and Bedrock adapters ship alongside — tested against recorded fixtures,
disabled by default. Switching to a hosted provider is a config change, not a rewrite.
That portability is deliberate: an enterprise adopting this would not accept a control
plane welded to one inference vendor.

### 8.5 Where the agent runs

The LLM runs on your machine; the guardrail runs on AWS. That is not a compromise, it is
the realistic enterprise topology — agents run on developer laptops, in CI, and in other
clouds, and a control plane must govern them wherever they are. The agent reaches the
deployed guardrail over ordinary outbound HTTPS.

M2 additionally ships the same agent as a Lambda, which can be pointed at a free
`cloudflared` tunnel to your local Ollama, demonstrating an AWS-resident agent under real
inference. That path is documented as demo-only.

---

## 9. AWS architecture

### 9.1 Deployed shape

```
   Reviewer ──► S3 static site      guardrail-console-dev-<account>
                (React console)     HTTPS via the REST endpoint, hash routing
                       │
                       │ x-api-key, pasted by the reviewer
                       ▼
   Agent + SDK ───────────────────► ┌──────────────────┐
   AWS-hosted agent Lambda ───────► │ Lambda Function  │  authType NONE
   MCP proxy ─────────────────────► │       URL        │  auth is in the application
                            └────────┬─────────┘
                                     ▼
                            ┌──────────────────┐
                            │  Lambda arm64    │  512 MB, 10s timeout
                            │  Mangum→FastAPI  │  log group: 7-day retention
                            │  + deps layer    │
                            └────────┬─────────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              ▼                      ▼                      ▼
      DynamoDB: audit         DynamoDB: decisions    DynamoDB: policies
      5 WCU / 5 RCU           5 WCU / 5 RCU          5 WCU / 5 RCU
      hash-chained            HITL + TTL             versioned bundles
              │
              │ Streams
              ▼
      S3 audit archive ──► SNS ──► reviewer notification
```

*(DynamoDB and SNS arrive in M1–M3; M0 deploys the compute and edge; the console
bucket arrives in M6. CloudFront is designed in and disabled — see step 2 of §4.)*

### 9.2 Why each service

| Service | Why this one | Free allowance |
|---|---|---|
| **Lambda** (arm64) | Scales to zero; arm64 is cheaper per GB-s and the free tier is denominated in GB-s | 1M req + 400k GB-s/mo, forever |
| **Function URL** | API Gateway's free tier expires after 12 months, then $1/million; this never charges. Also the whole edge -- CloudFront is optional | $0, forever |
| **CloudFront** | *Optional.* New AWS accounts cannot create distributions without a support case, so the architecture does not depend on it. Enable with `GUARDRAIL_ENABLE_CLOUDFRONT=true` for edge caching and a custom domain | 1 TB + 10M req/mo, forever |
| **S3 static hosting** | Serves the M6 React console over its **REST** endpoint, which is HTTPS. Website hosting is deliberately off: it is HTTP-only, and the page takes an API key. The console routes with `#/`, so one `index.html` serves every route with no rewrite rule and no index document | 5 GB for 12 months, then ~$0.001/mo at this size |
| **DynamoDB** | Conditional writes give race-free HITL resolution; single-digit-ms reads | 25 GB + 25 WCU/RCU **provisioned** |
| **CloudWatch** | Logs Insights carries the fine-grained analysis the metric budget cannot | 5 GB logs, 10 metrics, 10 alarms |
| **Cognito** | Console auth without building password handling | 10,000 MAU, never expires |
| **SSM Parameter Store** | Free secret storage (Secrets Manager is $0.40/secret/month) | Standard params free |

### 9.3 Deliberately excluded

NAT Gateway (~$32/mo), ALB (~$16/mo), ECS/Fargate/EC2/EKS, WAF ($5/mo), Secrets Manager,
ECR, provisioned concurrency, VPC endpoints. **There is no VPC at all** — Lambdas run
outside one, which is both free and faster to cold-start.

`scripts/check_banned_resources.py` fails CI if any of these appear in the synthesized
template. It reads `cdk synth` output rather than source, so it catches resources added
implicitly by an L2 construct's defaults — which is how a NAT gateway usually appears.

---

## 10. The policy lifecycle

Through M3 the running policy was whatever was baked into the deployment artifact. For a
governance product that is a real limitation: tightening a rule after an incident should
take seconds rather than a redeploy, and proving *which* policy was in force last Tuesday
should not mean cross-referencing a git log against a deploy history.

### Three steps, deliberately separate

```
  validate  ->  does this parse, and mean what I think?     changes nothing
  publish   ->  store it as an immutable numbered version   changes nothing
  activate  ->  make it the policy agents are judged by     changes everything
```

Publishing is inert, so CI can publish on every merge without risk. Activating is the
single deliberate act that alters behaviour.

**Rolling back is `activate` pointing at an older number.** There is no separate rollback
endpoint and no separate rollback code path — which matters more than it looks. A distinct
rollback route would be code that runs only during incidents, and that is the worst
possible test-coverage profile for the operation you most need to work. It also means a
rollback can only ever land on a bundle that was reviewed and stored.

### Storage

Versions live in the **existing audit table**, in their own partitions:

```
pk = TENANT#<tenant>#POLICY#<bundle_id>
  sk = VERSION#000000000003     immutable published bundle (canonical JSON)
  sk = ACTIVE                   pointer: {"active_version": 3, "activated_by": ...}
```

A separate table would need its own provisioned capacity, and 15 of the account's free
25 WCU are already committed. This design adds **zero** capacity and no index.

Three properties fall out of it:

* **The store assigns version numbers, never the file.** Two authors both typing
  `version: 2` would otherwise overwrite each other, and `bundle_version` in every audit
  record would stop identifying a specific policy.
* **Nothing is ever deleted.** The Lambda's IAM role has `PutItem`, `GetItem`, `Query`,
  and `UpdateItem` — deliberately not `DeleteItem`. The history is append-only *by
  permission*, not by convention.
* **Bundles are stored as canonical JSON strings**, exactly like audit payloads and for
  the same reason: DynamoDB cannot store floats, and a threshold of `1000.50` is ordinary.
  A lossy round trip would silently shift a threshold.

### Hot reload

Each warm Lambda container caches the active bundle and re-checks a small pointer item
every `GUARDRAIL_POLICY_REFRESH_SECONDS` (default 30). The bundle body is fetched only
when the version actually moved.

The timer is not a stylistic choice. Lambda freezes a container between invocations, so a
background poller or a subscription would never run — the only place to re-check is the
request path. The refresh window is therefore an explicit, tunable bound on how stale a
policy may be, rather than an accident.

### What happens when the policy store is unreachable

The rule is **keep serving the last known good bundle, indefinitely**. A policy-store
outage must not become an agent outage, and the previously active policy is a far better
answer than no policy.

One case is genuinely ambiguous: a cold start while the store is down, with no cached
bundle to fall back on. Three options, and the trade-off is real:

| Option | Consequence |
|---|---|
| Fail the request | Fail-closed clients block everything — the fleet halts because one DynamoDB call timed out |
| Allow everything | Never. This is the exact failure the product exists to prevent |
| Serve the packaged bundle | The reviewed policy that shipped with this build |

The third is chosen, with the caveat stated plainly: if the published policy was
*stricter*, this is a temporary loosening. So it is not silent. The event logs at error
level, `/readyz` returns 503 and says `DEGRADED`, and every decision made this way carries
the packaged bundle's id and version in its audit record — distinguishable forever after.

### Who may change policy

**An agent whose key can rewrite the policy governing it is not governed** — it can
approve its own next action by publishing a bundle that permits it. So writing policy is a
separate permission from calling `/v1/evaluate`:

* `GUARDRAIL_POLICY_ADMIN_KEY_IDS` lists the key ids allowed to publish or activate.
* It defaults to **empty, meaning nobody can**. Publishing then fails with a 403 naming
  the variable to set. That is deliberately inconvenient — the alternative default, "any
  authenticated caller may rewrite policy", is insecure the moment one agent key leaks,
  and insecure silently.
* **Reading** policy stays open to any authenticated caller. An agent knowing the rules it
  is bound by is not a risk; editing them is.

### Endpoints

| Method | Path | Who |
|---|---|---|
| `GET` | `/v1/policies` | any key — versions, and which is active |
| `GET` | `/v1/policies/active` | any key — the bundle actually being served, and its provenance |
| `GET` | `/v1/policies/versions/{n}` | any key — one stored version |
| `POST` | `/v1/policies/validate` | any key — lint without storing; returns 200 with `valid: false` |
| `POST` | `/v1/policies` | **admin** — publish (`?activate=true` to do both) |
| `POST` | `/v1/policies/versions/{n}/activate` | **admin** — activate, i.e. also rollback |

---

## 11. Simulation, dry run, and evidence

Every milestone before this one *claimed* the policy behaves a certain way. This chapter is
about making the claim executable.

### Three levels of "don't actually do it"

| Level | Set by | What happens |
|---|---|---|
| Per-request dry run | `dry_run: true` on the envelope | The engine evaluates and **records** normally; the SDK never executes the tool. The audit record is tagged and kept out of enforcement metrics |
| Bundle-wide shadow | `metadata.mode: shadow` | `block` and `require_hitl` are downgraded to `log_and_allow` for the whole bundle, so a policy can be trialled against live traffic before it restrains anything. Matched rules are still recorded, or shadow mode would teach nothing |
| Simulation | `POST /v1/simulate` | Evaluates against the active policy, a published version, or an inline candidate — and writes **nothing at all** |

**Why simulation is not audited.** `/v1/evaluate` records what an agent *attempted*; a
simulation is not an attempt. Recording thousands of what-ifs beside real decisions would
dilute the one log meant to answer "what did this agent do", forcing a reviewer to separate
evidence from speculation on every query. Simulations are still emitted as structured log
lines, so the activity stays visible in CloudWatch without entering the chain.

There is no code path from `/v1/simulate` to a state change, which is what makes it safe to
point change-impact analysis at production.

### The scenario harness

`guardrail-sim` runs YAML scenario files against a policy — offline against a bundle on
disk, or live against a deployed control plane, through one interface. Offline needs no
AWS, no credentials, and no network, which is what lets the conformance suite gate a pull
request before anything is deployed.

```yaml
- id: bulk-delete-blocked
  critical: true
  description: A delete above the blast-radius threshold is refused before it runs.
  action:
    tool: db.delete_records
    arguments: { table: users, count: 500 }
  expect:
    decision: block
    allowed: false
    rules: [db-bulk-delete]
    message_contains: "500"
```

**Scenarios assert the rule id, not just the outcome.** A scenario that only expected
`block` would keep passing after the rule it was written for was deleted, so long as some
other rule happened to block the same action — precisely the regression the suite exists to
catch, sailing through green.

Two other anti-vacuity measures, both learned the hard way on this project:

* An `expect:` block asserting nothing is **rejected at load time**. A test that cannot
  fail is worse than no test, because it reads like coverage.
* A run that finds **zero** scenarios raises instead of reporting a cheerful pass.
  "0 tests, all green" is the most misleading thing a gate can print.
* `guardrail-sim validate` checks every referenced rule id against the bundle. A typo in
  `expect.rules` fails loudly on its own, but a typo in `rules_absent` would pass forever —
  an id that does not exist can never match. Only this check finds that dead assertion.

### Commands

```
guardrail-sim validate scenarios/                 is the suite itself sound?
guardrail-sim run      scenarios/ --endpoint URL  does the live policy behave?
guardrail-sim parity   scenarios/ --endpoint URL  does dry run tell the truth?
guardrail-sim diff     scenarios/ --candidate F   what would this edit change?
```

`run` exits non-zero on any failure, which is what makes it a gate rather than a report
somebody remembers to read. It distinguishes a **failure** (the policy is wrong) from an
**error** (the endpoint was unreachable) — conflating them sends someone to edit a policy
file over what is actually a bad URL.

### Dry-run parity

`parity` runs every scenario twice — enforcing and dry-run — and diffs the decisions. This
is the claim that makes dry run worth anything: a shadow run that quietly evaluated
differently would give false confidence immediately before a policy change, which is worse
than having no dry-run mode at all.

### Change-impact analysis

`diff` evaluates the same corpus of actions under two bundles and reports every decision
that would change, **with a direction** — because "stricter" and "looser" are not the same
risk. Stricter is an availability problem: agents start being refused. Looser is an
exposure problem: something that was governed no longer is.

It also reports actions where the *decision* is unchanged but a **different set of rules**
fired. Two rules blocking the same action means deleting one changes nothing today and
everything tomorrow; a decision-only diff would show that edit as a no-op.

The honest limit is stated in the output: a diff compares over the actions you give it, so
"0 changes" means "nothing in this corpus exercised the edit", not "this edit is safe". The
corpus quality is the analysis quality — which is why the scenario suites double as the
corpus. They are the one set of actions someone has already thought about.

### Evidence artifacts

The same run renders four ways, for four readers: **console** for the engineer, **JUnit
XML** for CI, **HTML** for a reviewer or auditor, and **JSON** for anything else.

The HTML is deliberately self-contained — no CDN, no external stylesheet, no fonts. An
evidence artifact that renders differently in six months, or not at all offline, is not
evidence. Both the XML and the HTML record the **target** and the **policy bundle version**
behind every decision, so a green report can be re-derived rather than merely trusted.

---

## 12. Integrating with existing agents

The hardest question about a governance layer is not whether it works but whether anyone
can adopt it. Three integration points exist, and all three obey one rule.

### The rule

**Evaluate before dispatch, and refuse by returning a message the model can read.**

Raising an exception into a framework's tool loop usually aborts the run, turning a policy
refusal into an outage. Returning the refusal *as the tool result* keeps the agent alive
and lets it explain itself and choose something else. That is what makes a guardrail
usable rather than merely obstructive.

### MCP proxy — governing a server that knows nothing about this

The Model Context Protocol is how a growing number of agents reach their tools, and
`tools/call` is the moment an action happens. That makes it the ideal interception point,
and the only one where governance can be added to a **third-party** tool server with no
changes to it at all.

```
agent ──stdio──► guardrail-mcp ──stdio──► @modelcontextprotocol/server-filesystem
                      │
                      └── POST /v1/evaluate   (before anything is forwarded)
```

Everything except `tools/call` passes through untouched — `initialize`, `tools/list`,
resources, prompts, notifications, and anything MCP adds later. A proxy that only
understood the methods it was written against would silently break a server that added one.

A refusal is returned as a **result carrying `isError: true`**, not a JSON-RPC error. MCP
clients surface the first to the model as readable content and the second as a broken
server; a refusal is something an agent should reason about, not choke on.

The upstream server runs as the proxy's **own child process**, so there is no other path to
it. That is what turns "the proxy enforces policy" into "the proxy is the only way in".

`scripts/mcp_demo.py` proves it end to end against the real published server. A canary
string is written into a fake private key: if the blocked read had reached the server, the
key's contents would have come back through the proxy. Asserting the canary is **absent** is
what separates enforcement from an audit log — a proxy that forwarded first and logged
afterwards would still report `block` and still pass every other check.

### OpenAI-compatible tool loops

`GuardedToolDispatcher` wraps any function of the shape "given a tool name and arguments,
run it" — which covers Ollama, Groq, vLLM, and the OpenAI SDK unchanged. The wrapped
function is not called at all when policy refuses.

### LangChain, and an honest limitation

`guard_langchain_tool` wraps a tool's own callable, which is where a check can actually
stop something.

`GuardrailCallbackHandler` is also provided, and is **documented and tested as detection,
not enforcement**: `on_tool_start` is a notification, and LangChain offers a callback no
supported way to veto the tool it is announcing. Shipping that alone and calling it
governance would be a component that looks like a control while only observing — which is
worse than no component, because it manufactures confidence.

---

## 13. Hardening: limits, failure, and cost

### Rate limiting, and what it actually guarantees

Per-tenant token buckets, held **in-process** in each warm container.

The plan called for a DynamoDB bucket. At 5 provisioned WCU that costs one write per
request, caps the service near 5 requests/second, and spends the very capacity the audit
chain needs — protecting a service from the constraint it is already bound by.

The honest consequence: with `N` containers the true global bound is `N x per-container
rate`, not the per-container rate. `global_ceiling()` returns that number and `/readyz`
reports it. **It is not a fair-share mechanism**, and it is not described as one.

### What happens under overload

At sustained load the audit table reaches its provisioned ceiling and writes throttle. The
service then returns **503 with `Retry-After`** rather than a decision, and a fail-closed
client blocks the action.

That is deliberate. Permitting an action that could not be recorded is exactly the gap this
system exists to close — so availability is traded for safety, knowingly. It also means an
attacker who can saturate the guardrail can stop governed agents from acting, which is
recorded in the [threat model](docs/threat-model.md) rather than left to be discovered.

### Measured behaviour

| | |
|---|---|
| Sustained throughput | **6.1 req/s** for 120 s, zero errors, zero throttles |
| Policy engine latency | p50 **4.3 ms**, p99 **11 ms** |
| Round-trip latency | p50 314 ms, p99 349 ms |
| Beyond capacity | clean 503 + `Retry-After`, **0 Lambda platform errors** |

The two latency numbers answer different questions. The engine figure is what an agent pays
for *governance*. The round-trip adds the public internet, TLS, and the DynamoDB write, and
is dominated by network distance to `us-east-1`.

**A short load test lies here.** A 30-second run reported 18 req/s with zero errors — that
is DynamoDB burst credit, not capacity. Only a run past ~120 seconds reaches the real
ceiling. Full report: [`reports/loadtest.md`](reports/loadtest.md).

### Failure injection

`tests/unit/test_chaos.py` breaks the system on purpose, because "fails closed" is the
load-bearing safety claim and is never exercised by normal operation. Five real outages —
network partition, DNS failure, timeout, 503, and an unwritable audit chain — across four
call paths, each asserting that **no side effect occurred**.

It also pins two subtler properties: the circuit breaker fails *fast*, never *open* (if it
short-circuited to allow, a brief outage would become a total governance bypass), and a
malformed `200` is never read as an allow.

### Portability

The same conformance suite that passes against deployed AWS passes **20/20 against a plain
container**, with no code change — only the entrypoint differs (`uvicorn` here, `Mangum` on
Lambda). Run `uv run python scripts/portability_proof.py`; CI runs it too.

This is checked rather than claimed because the claim had been false. The Dockerfile
asserted portability from M0 and had never been built; when it finally was, the image did
not start.

### Cost, enforced rather than remembered

| Allowance | Used |
|---|---|
| DynamoDB provisioned capacity | **15 of 25** WCU / RCU — one table, two indexes |
| CloudWatch custom metrics | **10 of 10** — full, enforced by a test |
| CloudWatch alarms | **7 of 10** |
| CloudWatch dashboards | **1 of 3** |
| Actual AWS spend | **$0.00** |

Two limits turned out to be lower than the plan assumed, and both changed the design:
per-function **reserved concurrency cannot be set at all** on this account (the total quota
is 10, and AWS requires 10 to stay unreserved), and a **`prod` stage cannot coexist with
`dev`** without exceeding the free 25 WCU.

---

## 14. Repository layout

```
packages/guardrail-core/      Policy engine. Pure: no I/O, no AWS, no network.
                              Every function is a function of its inputs, which is what
                              makes decisions reproducible and the engine 100% unit-testable.

packages/guardrail-service/   FastAPI control plane.
  app.py                      App factory, request-id middleware, uniform error envelope
  config.py                   All configuration from the environment
  handler.py                  Mangum entrypoint — the ONLY Lambda-specific file
  observability.py            Powertools logger/metrics, lazy tracer, metric budget
  routers/                    HTTP endpoints

  auth.py                     API keys, and the separate policy-admin permission
  policy_provider.py          Hot-reloading cache of the active bundle
  storage/                    Audit chain, HITL decisions, versioned policy bundles

packages/guardrail-sdk/       Client, @governed_tool, circuit breaker, blocking HITL wait
packages/guardrail-sim/       Scenario DSL, runner, change-impact diffing, evidence reports
apps/console/                 M3 review console (single self-contained HTML file).
                              Kept as a zero-dependency fallback: it needs no build step
                              and runs straight off disk.
apps/console-ui/              M6 React console, deployed to S3
  src/lib/api.ts              Typed client, session handling, honest error surfacing
  src/lib/store.tsx           Session state, capability gating, hash routing
  src/components/             shadcn-shaped primitives + Aceternity-style effects
  src/pages/                  The six screens
  src/__tests__/              Behavioural tests, incl. the permission gate
apps/demo-agent/              Qwen3-powered agent with five governed tools

infra/                        AWS CDK (Python)
scripts/                      The cost gate, preflight, API key minting
policies/                     Default YAML policy bundle
scenarios/                    Conformance scenarios (success criteria + model properties)
tests/                        Unit and integration suites
```

**The layering rule:** `guardrail-core` may not import `guardrail-service`, boto3, or any
network library. Policy evaluation must remain pure — testable offline, identical in every
host, and impossible to make accidentally dependent on infrastructure.

---

## 15. Running it

### Locally

```bash
uv sync --all-extras
uv run pytest
uv run uvicorn guardrail_service.app:app --port 8080

curl http://localhost:8080/healthz
curl http://localhost:8080/readyz
open http://localhost:8080/docs        # interactive OpenAPI
```

### As a container

```bash
docker compose up --build
curl http://localhost:8080/healthz
```

Ollama is deliberately **not** in compose: on Windows it loses GPU access in a container
unless WSL2 + CUDA passthrough is configured, making Qwen3 markedly slower for no benefit.
Run it natively; containers reach it at `host.docker.internal:11434`.

### Validating infrastructure without AWS or Docker

```bash
cd infra
GUARDRAIL_SKIP_BUNDLING=1 GUARDRAIL_STAGE=dev \
  CDK_DEFAULT_ACCOUNT=000000000000 CDK_DEFAULT_REGION=us-east-1 \
  uv run python app.py

cd .. && uv run python scripts/check_banned_resources.py infra/cdk.out
```

### Deploying

See **`PROGRESS.md` §7** for the full prerequisite list and commands.

---

## 16. The review console

`apps/console-ui` — React 19 + Vite + TypeScript, deployed to S3 and talking to the
control plane over HTTPS. It is the human surface of everything in this document.

### What each page is for

| Page | Answers |
|---|---|
| **Overview** | What an action guardrail is, how it differs from a text guardrail, and what has actually been proven. Readable without a credential — it is what a first-time reader lands on |
| **Policy Studio** | Read, author, validate, publish, activate, roll back. Publishing and activating are separate controls on purpose |
| **Change Impact** | Runs the corpus through the active policy and a candidate and reports where they disagree — calling out which actions would become *permitted* |
| **Playground** | Probes any version or draft, and names rules that matched **nothing** across the whole corpus |
| **Dry-run & Shadow** | The three levels of evaluate-without-enforcing, and a parity run on the enforcement path proving dry-run reports what enforcement really does |
| **Conformance** | The real `scenarios/*.yaml` corpus, compiled in at build time, run against live AWS |
| **MCP Proxy** | How a third-party tool server is governed with no changes to it, and what the leak canary proves that a browser cannot |
| **Agent Console** | Runs the AWS-hosted agent against a live model and shows the governed transcript: every tool it tried, the rule that fired, and the audit sequence number the decision landed at |
| **Decision Theatre** | Sends any tool call to `/v1/evaluate` or `/v1/simulate` and shows the verdict, every matching rule, the derived facts, and the latency |
| **Review Queue** | The HITL surface: approve or deny an action held before execution, with a reason, under a live countdown to the timeout |
| **Audit & Chain** | Every decision, and the chain verdict from `/v1/audit/verify` — with the chain's limits stated on the page rather than buried in a document |
| **System Health** | `/healthz`, `/readyz`, the policy in force, and the free-tier ceilings that shape the design |

### Twelve screens, three groups

*Operate* is day-to-day work: run agents, judge actions, answer for them. *Policy* is
changing the rules, and knowing what a change would do first. *Evidence* is the claims
this project makes, executed rather than asserted. They are usually three different
people, and a reviewer approving a held action should not have to read past the
policy-authoring tools to find the queue.

### Publishing is not activating

The API supports `POST /v1/policies?activate=true`. The console deliberately does not use
it. Publishing stores an immutable version that governs nobody; activating changes what
every agent in the tenant may do, immediately, with no deploy. One control that did both
would make "save my draft" and "change the rules the system enforces" the same gesture.

Rolling back is activating a lower version — there is no separate rollback path anywhere
in this system, because a distinct one would be code that runs only during incidents,
which is the worst possible test-coverage profile for the operation you most need to work.

### Finding rules that defend nothing

The Playground runs every corpus action against the selected policy and names the rules
that never matched. A rule that matches nothing looks exactly like coverage: it sits in
the file, reads convincingly, is counted in "12 rules", and defends nothing. That failure
mode has happened three times in this repository's own tests.

Some of the class is caught earlier — a rule naming a nonexistent derived fact is rejected
at load time, an invalid regex fails on upload, an unknown operator is a load-time error.
What survives is the rule that is *valid* and still inert: a threshold nothing reaches, a
tool nobody calls, a condition that is always UNKNOWN.

### The console's scenario corpus is generated, not copied

`apps/console-ui/src/generated/scenarios.json` is compiled from `scenarios/*.yaml` by a
prebuild step that every npm script runs first, and `test_console_scenarios.py` fails if
the two ever disagree. A conformance report built from a hand-maintained copy would
eventually report green against scenarios nobody enforces — worse than no report.

The canonical runner remains `guardrail-sim`, which gates a pull request, validates the
DSL far more strictly, runs offline and against a container, emits JUnit XML, and contains
the test that deletes a rule and requires the suite to go red. The console page says so
rather than letting a green tick imply it is the gate.

### The side-effect ledger

The Agent Console renders what the tools *actually did*, from the agent's own ledger.
Without it, "blocked" is a claim the agent makes about itself. With it, "blocked" is
checkable against "nothing happened" — which is the difference between a demo and
evidence.

### `/v1/me`, and why a UI needs it

The console asks the server what the presented key may do, and renders only that.
Guessing gets it wrong in one of two ways: guess too permissive and it shows an Approve
button that always 403s, teaching the reviewer that the control is broken rather than that
they lack the role; guess too restrictive and it hides a control the operator legitimately
holds, which during an incident is the more expensive mistake.

`capabilities` is a flat set of verbs rather than the role name, because the server has a
grant the role does not describe: a key with the `agent` role named in
`GUARDRAIL_POLICY_ADMIN_KEY_IDS` really can publish policy. A client deriving permissions
from the role string alone would tell that operator they cannot do the thing they can.

The list is kept honest by `test_capabilities_agree_with_what_the_api_enforces`, which
does not read the capability function at all: it asks the server what the caller may do,
then goes and tries every verb, and fails if the two disagree **in either direction**.

### Authentication, stated plainly

The reviewer pastes their own API key. It is held in `sessionStorage`, sent as
`x-api-key`, and **stored only after `/v1/me` accepts it** — persisting an unverified key
produces a console that looks connected and is not, and every page then renders an empty
table that reads as "no activity" rather than "you are not authenticated".

No credential is ever baked into the bundle. Only URLs are, and CI fails the build if a
key-shaped string appears in `dist/`, because a deployed frontend is a world-readable
artifact.

The honest limitation: an XSS on this page could read the key. Cognito hosted sign-in is
the designed upgrade and is deferred rather than forgotten — see `docs/threat-model.md`,
gap 6.

### Why not build the components from a registry

The visual language borrows the Aceternity UI patterns (aurora, spotlight, meteors,
glowing borders, word-by-word text reveal) and the shadcn/ui component shape, both
rewritten in `src/components/`. Each is a handful of divs and a keyframe, and the console
must build with no network access and no CDN: the deployed page is fully self-contained,
which matters for a security tool whose bundle is public.

Every ambient effect is `aria-hidden` and conveys nothing that is not also written in
text, all animation stops under `prefers-reduced-motion`, and the four outcomes are never
distinguished by colour alone — each badge carries its word, `block` is filled where
`allow` is outlined, and `require_hitl` pulses because it is the one outcome that is not
yet settled.

---

## 17. Roadmap

| Milestone | Delivers | Exit criterion |
|---|---|---|
| **M0** ✅ | Foundation, CI/CD, cost gate, health endpoints | Live `/healthz` returns the build SHA; raw origin returns 403 |
| **M1** ✅ | Policy engine, `/v1/evaluate`, hash-chained audit | 500-record delete blocked, 5-record allowed, external email → HITL, internal → allowed, confidential read → log_and_allow — all against the deployed URL |
| **M2** ✅ | SDK + `@governed_tool` + Qwen3 demo agent | A real LLM-generated tool call is blocked pre-execution by the deployed guardrail |
| **M3** ✅ | HITL workflow + review console | Agent pauses; reviewer approves in a browser; agent resumes. Deny and timeout paths both shown |
| **M4** ✅ | Simulation harness, dry-run, policy versioning | Green conformance report against prod; dry run executes nothing; a new bundle version changes behaviour with no redeploy |
| **M5** ✅ | Hardening, multi-tenancy, MCP proxy, load test | MCP proxy governs an off-the-shelf MCP server that knows nothing about Guardrail |
| **M6** ✅ | React console on AWS: overview, agent console, decision theatre, review queue, audit chain, system health, plus `/v1/me` | A judge opens an HTTPS URL, pastes a key, runs the AWS-hosted agent, watches an action be held, and approves it — with no laptop of ours involved |
| **M7** ✅ | Policy Studio, change-impact diff, policy playground, dry-run and shadow, conformance report, MCP proxy view | Verified live: publishing v4 changed nothing, activating it flipped a 150-row delete from `block` to `allow` with no redeploy, and activating v3 restored it |

### On the bonus requirement

The problem statement's bonus asks for a dry-run mode. It is delivered at three levels —
per-request, bundle-wide shadow, and side-effect-free simulation — plus **shadow-policy
diffing**, which evaluates a candidate bundle alongside the active one and reports which
decisions would change. See §11 for how each differs and why simulation is deliberately
kept out of the audit chain.
