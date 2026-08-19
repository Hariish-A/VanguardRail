# Guardrail — Progress Log

> **Purpose of this file.** A complete, self-contained record of what has been built,
> what was decided and why, and what remains. It is written to be pasted into a fresh
> LLM session as full context with no prior conversation — so it restates decisions
> rather than referring back to them.

**Project:** Guardrail — an action-layer guardrail for AI agents (problem statement PS-3.1).
**Repository root:** `d:\Official\Projects\Guardrial`
**Last updated:** end of Milestone 5.

---

## 0. Resume here — operational runbook

> Everything needed to pick this up cold. `CLAUDE.md` carries the short version and is
> loaded automatically each session; this is the long form.

**Live system**

| | |
|---|---|
| Base URL | `https://y5ycfqeeilb24ylgmsse2agl5i0njovv.lambda-url.us-east-1.on.aws` |
| AWS account / region | `182355603382` / `us-east-1` |
| Lambda / table | `guardrail-service-dev` / `guardrail-audit-dev` |
| Capacity in use | 15 WCU / 15 RCU of the free 25 |
| Credentials | in `.env` (git-ignored) — never hardcode them into a tracked file |

**Prerequisites**

* Docker Desktop **running** (Lambda bundling needs it; it stops on its own — check
  `docker info` before blaming the code)
* `ollama serve` running with `qwen3:latest`, for the agent only
* AWS CLI and CDK CLI on PATH — both installed under the user's profile, so a **freshly
  opened terminal** is required for them to resolve

**Quality gate — all four before any commit**

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy packages
uv run pytest
```

**Deploy**

`.env` now carries every deploy variable, so sourcing it is the whole command:

```bash
cd infra
set -a && . ../.env && set +a
GUARDRAIL_STAGE=dev GUARDRAIL_VERSION=$(git rev-parse HEAD)   GUARDRAIL_CONSOLE_ORIGINS="http://localhost:5173,http://127.0.0.1:5173"   cdk deploy Guardrail-Service-dev --require-approval never --outputs-file outputs.json
```

**`GUARDRAIL_API_KEYS_JSON` must be single-quoted in `.env`.** Unquoted, `. .env` strips
the inner double quotes and the deployed table is unparseable JSON — the app then fails
closed and returns **401 to every caller**, with nothing in the deploy output explaining
why. This cost a deploy cycle during M4, so `infra/stacks/service_stack.py::_api_key_table`
now validates the value at synth and fails the build with a message naming the cause.

The variable maps **SHA-256 hashes** to caller metadata; the plaintext key never leaves
the machine it was minted on. Mint with
`uv run python scripts/generate_api_key.py --tenant acme --merge '<current json>'`.

**`GUARDRAIL_POLICY_ADMIN_KEY_IDS`** lists the key ids allowed to publish or activate
policy. It defaults to **empty, meaning nobody can** — an agent whose key could rewrite
the policy governing it would not be governed. Currently set to `acme-policy-admin`.

Three keys are deployed: `acme-7b6d7d20` (reviewer-one, the M3 console), `acme-sim`
(the conformance harness, evaluate-only), and `acme-policy-admin` (policy changes).

If `cdk deploy` fails with jsii `ENOTEMPTY` on Node 22, synth already succeeded and only
cleanup broke: clear `%TEMP%\jsii-kernel-*` and retry.

**Verify a deploy**

```bash
BASE=https://y5ycfqeeilb24ylgmsse2agl5i0njovv.lambda-url.us-east-1.on.aws
KEY=<from .env>
curl -s $BASE/healthz                                   # version == deployed git SHA
curl -s "$BASE/v1/audit/verify" -H "x-api-key: $KEY"    # chain_valid: true
curl -s "$BASE/v1/decisions"    -H "x-api-key: $KEY"    # pending review queue
curl -s "$BASE/v1/policies"     -H "x-api-key: $KEY"    # published versions + active one
curl -s "$BASE/readyz"                                  # now a real check; can return 503
```

**Conformance harness (M4)** — the fastest way to prove the whole system works:

```bash
set -a && . ./.env && set +a
BASE=https://y5ycfqeeilb24ylgmsse2agl5i0njovv.lambda-url.us-east-1.on.aws

# Offline, no AWS, no credentials -- this is what gates a pull request.
uv run guardrail-sim run scenarios/ -v

# Against live AWS, with evidence artifacts.
uv run guardrail-sim run scenarios/ --endpoint "$BASE" --api-key "$GUARDRAIL_API_KEY"   --junit reports/conformance.xml --html reports/conformance.html

# Prove dry-run reports what enforcement would really do.
uv run guardrail-sim parity scenarios/ --endpoint "$BASE" --api-key "$GUARDRAIL_API_KEY"

# Change-impact analysis before publishing an edit.
uv run guardrail-sim diff scenarios/ --endpoint "$BASE" --api-key "$GUARDRAIL_API_KEY"   --candidate path/to/candidate.yaml
```

**Publish / activate / roll back policy** (needs the admin key):

```bash
uv run python -c "import json,yaml,pathlib;print(json.dumps({'bundle':yaml.safe_load(pathlib.Path('policies/default.yaml').read_text())}))" > /tmp/pub.json
curl -s -X POST "$BASE/v1/policies" -H "x-api-key: $GUARDRAIL_POLICY_ADMIN_API_KEY"   -H 'content-type: application/json' -d @/tmp/pub.json
curl -s -X POST "$BASE/v1/policies/versions/2/activate" -H "x-api-key: $GUARDRAIL_POLICY_ADMIN_API_KEY"
# rollback is the same call with a lower version number
```

Warm containers pick up an activation within **30 seconds**
(`GUARDRAIL_POLICY_REFRESH_SECONDS`); the container that served the activation sees it
immediately.

**Run the demo**

```bash
uv run python -m demo_agent "delete all 500 inactive user accounts"   # -> blocked
cd apps/console && python -m http.server 5173 --bind 127.0.0.1        # review console
```

---

## 1. What this project is

Every commercial guardrails product filters LLM **text**. None govern what an agent
**does** after the text is produced. A perfectly clean model response can still instruct
a tool to delete 10,000 database rows, email a competitor, or read a confidential file.

Guardrail is the missing layer: a service that evaluates a **tool call** against a
declarative policy bundle *before* it is dispatched, returning one of four outcomes —
`allow`, `log_and_allow`, `require_hitl`, `block` — and writing a tamper-evident audit
record for every decision.

### Hard constraints that shape every decision

1. **Zero AWS spend.** Only always-free AWS services. This is enforced by a CI gate, not
   by discipline (see §4).
2. **Free, open-weight LLM.** Ollama running `qwen3:4b` locally. No paid API, no key.
3. **Every milestone ends deployed.** Each of M0–M5 produces a working, deployed
   artifact rather than accumulating toward one big release at the end.

---

## 2. Milestone status

| Milestone | Scope | Status |
|---|---|---|
| **M0** | Foundation, CI/CD, first deployable service | **DEPLOYED AND LIVE** (see §3.0) |
| **M1** | Policy engine, `/v1/evaluate`, hash-chained audit log | **DEPLOYED AND VERIFIED** (see §3.9) |
| **M2** | Enforcement SDK + Qwen3 agent governed by the deployed control plane | **COMPLETE AND VERIFIED** (see §3.10) |
| **M3** | Human-in-the-loop workflow + review console | **DEPLOYED AND VERIFIED** (see §3.11) |
| **M4** | Simulation harness, dry-run mode, policy versioning | **DEPLOYED AND VERIFIED** (see §3.12) |
| **M5** | Hardening, multi-tenancy, MCP proxy, load test | **DEPLOYED AND VERIFIED** (see §3.13) |

---

## 3. Milestone 0 — what was built

### 3.0 DEPLOYED — live on AWS

**Account:** 182355603382 · **Region:** us-east-1 · **Stage:** dev
**Base URL:** `https://y5ycfqeeilb24ylgmsse2agl5i0njovv.lambda-url.us-east-1.on.aws`
**Edge:** Lambda Function URL with CORS (CloudFront intentionally not used -- see below)
**Function:** `guardrail-service-dev` · **Deployed SHA:** `808dc4012fd0a87f54a72ee807b816e9cee7d266`

Verified against the live endpoint:

| Check | Result |
|---|---|
| `GET /healthz` | 200, `version` equals the deployed commit SHA exactly |
| `GET /readyz` | 200, all three dependencies enumerated |
| `GET /version` | 200 |
| `x-request-id` echo | Caller-supplied id survives the round trip |
| Lambda duration | **3.5-4 ms** (billed 4 ms), max memory 92 MB of 512 MB |
| Cold start | ~3.0 s |
| Log retention | 7 days, confirmed on the live log group |
| Budget `guardrail-zero-spend` | Active, $1 limit |

Wall-clock latency measures ~0.75 s from India, which is network RTT to us-east-1, not
compute. At 4 ms billed and 512 MB, the 400,000 GB-second allowance covers roughly
200 million invocations -- so the 1M **request** limit binds long before compute does.

**Memory note:** 92 MB used of 512 MB provisioned. Memory could drop to 256 MB and halve
GB-seconds, but Lambda scales CPU with memory and cold start would suffer. Compute is
nowhere near the binding constraint, so 512 MB stays.

#### CloudFront was dropped from the architecture (not merely deferred)

`EdgeMode = function-url-direct`, and that is now the **default**, not a fallback.

The original design put CloudFront in front with an Origin Access Control. A brand-new
AWS account cannot create distributions at all:

```
Your account must be verified before you can add new CloudFront resources.
(Service: CloudFront, Status Code: 403)
```

Fixing that needs a free AWS Support case and 24-48 hours. Rather than make the
architecture depend on a support queue, security moved to where it actually belongs.

**The reasoning.** Hiding the origin was defence in depth, never the real control. A
public HTTPS endpoint whose every request is authenticated is the normal shape of a
production API -- API Gateway endpoints are public too. What matters is authentication,
and that is application-level work we were doing regardless:

| Caller | Control | Milestone |
|---|---|---|
| Agents | Hashed API keys, or SigV4 (`authType: AWS_IAM`) | M2 / M5 |
| Console users | Cognito JWT validated in-app | M3 |
| Abuse | Reserved concurrency + per-tenant token bucket | M5 |

**CORS is configured on the Function URL**, not in FastAPI middleware, so preflight is
answered at the edge without paying a Lambda cold start and the app cannot accidentally
widen the policy. Verified live:

```
Origin: http://localhost:5173   -> Access-Control-Allow-Origin returned
Origin: https://evil.example.com -> no CORS headers, browser blocks it
```

**The M3 console** will be hosted on **GitHub Pages** (free, HTTPS, no AWS verification)
rather than S3 + CloudFront. S3 static website hosting is not viable: its website
endpoint is HTTP-only, which breaks Cognito.

**CloudFront remains one flag away** if the account is ever verified --
`GUARDRAIL_ENABLE_CLOUDFRONT=true` restores the distribution and switches the Function
URL back to `AWS_IAM`. `BaseUrl` is emitted either way, so the SDK, smoke tests, and
console never learn which edge is active. Both modes are covered by tests.

#### The security tripwire, rewritten

The earlier tripwire asserted "restore CloudFront before shipping data endpoints". With
a deliberately public edge that is the wrong invariant. It now asserts the right one:
`test_every_data_endpoint_is_authenticated` walks the FastAPI route table and **fails the
build** if any route that is not explicitly listed as public lacks an auth dependency.
So M1's `/v1/evaluate` cannot ship unauthenticated.

### 3.1 Verified working locally

All of the following were run and confirmed, not merely written:

- `uv run pytest` → **36 passed**
- `uv run ruff check .` → clean
- `uv run ruff format --check .` → clean
- `uv run mypy packages` → clean under `strict = true`
- `uv run uvicorn guardrail_service.app:app` → live server answering on all endpoints
- `cd infra && python app.py` → synthesizes 2 CloudFormation templates
- `python scripts/check_banned_resources.py infra/cdk.out` → passes; **8 resources, none paid**

Live responses confirmed:

```
GET /healthz  → 200 {"status":"ok","version":"0.0.0-dev","stage":"local","uptime_seconds":3.531}
GET /readyz   → 200 {"ready":true,...,"dependencies":[audit_table, decisions_table, policies_table]}
GET /version  → 200 {"version":"0.0.0-dev","stage":"local","service":"guardrail"}
```

Structured log line actually emitted:

```json
{"level":"INFO","message":"request","service":"guardrail","request_id":"live-check-77",
 "path":"/version","method":"GET","status_code":200,"duration_ms":5.28}
```

### 3.2 Not yet done *(as at the close of M0 — all since resolved)*

> Historical record of where M0 stopped. Both items were closed during M1: the stack
> has been deployed to AWS many times since, and Docker bundling is exercised on every
> deploy. Kept as written so the milestone log stays a log rather than a redraft.

**The stack had never been deployed to AWS**, because the AWS account did not exist while
M0 was built. Everything needed to deploy is written and synthesizes correctly; §7 lists
exactly what is required to run the deploy.

Docker Desktop was not running during development, so the container image was **not built
or run**. The Dockerfile is written but unverified — see §8.

### 3.3 Files created

```
Guardrial/
├── pyproject.toml                    uv workspace root; ruff/mypy/pytest config
├── uv.lock                           pinned dependency graph (committed)
├── .gitignore                        .env excluded from the first commit
├── .env.example                      every value you must supply, with provenance notes
├── Dockerfile                        portable container (NOT the Lambda artifact — see §5.3)
├── docker-compose.yml                local stack: service + DynamoDB Local
├── PROGRESS.md                       this file
├── Project Documentation.md          concepts, pipeline, architecture, how it all works
│
├── packages/
│   ├── guardrail-core/               pure policy engine — no I/O, no AWS, no network
│   │   └── src/guardrail_core/
│   │       ├── __init__.py
│   │       └── effects.py            Effect enum; ordered so `max()` = most restrictive
│   └── guardrail-service/            the control plane
│       └── src/guardrail_service/
│           ├── app.py                FastAPI factory, request-id middleware, error envelope
│           ├── config.py             pydantic-settings; all config from environment
│           ├── handler.py            Mangum entrypoint — the ONLY Lambda-specific file
│           ├── observability.py      Powertools logger/metrics; lazy tracer; metric budget
│           └── routers/health.py     /healthz, /readyz, /version
│
├── infra/                            AWS CDK (Python)
│   ├── app.py                        entrypoint; stage wiring; explicit outdir
│   ├── cdk.json
│   ├── placeholder/index.py          synth-without-Docker asset (never deployable)
│   └── stacks/
│       ├── service_stack.py          Lambda + Function URL + CloudFront + log group
│       └── budget_stack.py           $1 zero-spend tripwire with forecast alarm
│
├── scripts/
│   └── check_banned_resources.py     the CI cost gate
│
├── tests/unit/
│   ├── test_health.py                endpoints, request-id correlation, log structure
│   ├── test_effects.py               effect ordering and wire parsing
│   └── test_banned_resources.py      proves the cost gate actually fails on bad input
│
└── .github/workflows/ci.yml          quality → cost gate → deploy → smoke test
```

---

## 3.10 Milestone 2 — COMPLETE AND VERIFIED

### Exit criterion, met live

`uv run python -m demo_agent "Clean up our database by deleting all 500 inactive user
accounts from the users table."` against the deployed control plane:

* Qwen3 (8B, local Ollama) emitted `db_delete_records{table: users, count: 500,
  where: "status = 'inactive'"}`
* the SDK blocked it **before execution** -- `db-bulk-delete` and
  `destructive-tool-in-production` both matched
* **side effects: none** -- the tool body never ran
* audit seq 12 recorded on live DynamoDB
* the agent then explained the refusal by name and proposed batching, narrowing the
  query, or requesting an exception

That last point is the one worth keeping: the refusal is fed back to the model **as a
tool result**, not raised as an error. A block that crashes the agent gives the user a
stack trace; a block the model can read turns the guardrail into part of the conversation.

### What was built

```
packages/guardrail-sdk/
  client.py      pooled HTTP, jittered retries, circuit breaker, fail-closed default
  decorator.py   @governed_tool -- the pre-execution enforcement point
  exceptions.py  ActionBlocked / ApprovalRequired / GuardrailUnavailable
  models.py      typed Decision mirroring the service contract

apps/demo-agent/
  llm.py     OpenAI-compatible provider layer (Ollama default)
  tools.py   five governed tools + a SIDE_EFFECTS ledger
  agent.py   tool-calling loop that feeds refusals back to the model
  __main__.py CLI
```

**196 tests passing** at the close of M2; ruff and mypy --strict clean.
(Current total is 315 — see §3.12.)

### Scope CANCELLED, and why

Recorded here so neither resurfaces as a phantom TODO:

**SigV4 request signing in the SDK — cancelled as obsolete.** It existed to sign requests
to a Function URL with `authType: AWS_IAM` sitting behind CloudFront. CloudFront was
removed in M1 (new AWS accounts cannot create distributions without a support case), so
the URL is `NONE` + API-key auth. There is nothing to sign. If
`GUARDRAIL_ENABLE_CLOUDFRONT=true` is ever used, this returns as real work.

**Bedrock adapter — cancelled as incompatible with the project's constraints.** Bedrock
bills per token and is on the cost gate's banned list. It cannot be used at $0.

**Lambda-hosted agent behind a cloudflared tunnel — declined.** It would satisfy the
rubric's "agents also hosted on AWS" line literally, but: it adds no governance
capability (the guardrail is an HTTPS API and does not care where callers live); it makes
every demo depend on a tunnel from a laptop, whose URL regenerates on each restart; it
adds two internet round trips before the guardrail is even consulted; and a quick tunnel
**exposes local Ollama to the public internet with no authentication**. The standing
framing is stronger anyway: Guardrail governs agents wherever they run, from a control
plane on AWS. Roughly 30 minutes to build if a judge asks for it.

### The one real gap, now closed

The project had claimed repeatedly that switching inference providers is
configuration-only. Nothing verified it: the agent tests use a scripted LLM that bypasses
`LLMProvider._parse` entirely, so the single function absorbing provider differences had
no direct coverage.

`tests/unit/test_llm_providers.py` now parses recorded response bodies from Ollama, Groq,
and OpenAI and asserts all three yield an identical `Turn`, plus the malformed cases
(unparseable arguments, non-object arguments, missing name, no choices). Fixture
provenance is stated in the module docstring: the **Ollama fixture was captured from a
real local call**; the Groq and OpenAI fixtures are constructed from those providers'
documented response schemas, since calling them requires API keys this project
deliberately does not have.

### Measurement worth keeping

Qwen3 reasons before answering unless told not to. On this machine, with a five-tool
prompt, thinking exceeded a 300-second timeout; `/no_think` returned a correct tool call
in **34 seconds**. Reasoning traces add little to "which function, which arguments", so
it is disabled by default and re-enabled with `GUARDRAIL_LLM_THINKING=1`.

---

## 3.11 Milestone 3 — DEPLOYED AND VERIFIED

**Console:** `apps/console/index.html`, served at `http://localhost:5173`
**API:** `GET /v1/decisions`, `GET /v1/decisions/{id}`, `POST /v1/decisions/{id}/resolve`

### Verified live

| Path | Result |
|---|---|
| External email | `require_hitl`, 900s window, `on_timeout=deny` |
| Appears in queue | full arguments, matched rule, countdown |
| Approve | `allows_execution=true`; agent poll agrees; queue empties |
| Second resolve | **409** -- "already approved by reviewer-one" |
| Deny | `allows_execution=false` |
| Audit chain | seq 16 "approved by reviewer-one", seq 18 "denied" |
| `GET /v1/audit/verify` | `chain_valid: true`, 18 records |

### Design decisions

**Decisions share the audit table.** A second table needs its own provisioned capacity
and 15 of the free 25 WCU were already committed. Pending records live under a
`DECISION#` sort key and reuse the outcome index as a **sparse index** -- index
attributes are written only while pending and removed atomically on resolution, so
resolved decisions leave the queue with no scan, no filter, and no extra capacity.
Confirmed still **15 WCU / 15 RCU** after M3.

**Expiry is computed, never delegated to TTL.** DynamoDB reclaims items on a best-effort
schedule that can lag 48 hours. Whether a decision has expired is decided by comparing
timestamps on every read; the TTL attribute is set *seven days out*, deliberately far
beyond the review window, so an expired decision is still readable and reportable rather
than 404-ing at an agent that is polling it.

**Silence is never consent.** `on_timeout` defaults to `deny`.

**The console is one self-contained HTML file.** No build step, no `node_modules` in a
Python monorepo, no external requests. Reviewers paste their own API key, held in
sessionStorage -- the personal access token pattern: the credential belongs to the person,
is never embedded in the page, and dies with the tab. Cognito hosted sign-in is deferred
to M5, where it belongs alongside per-tenant key rotation.

### Bug found by the deploy

`resolve` returned 500 on first use: the IAM grant listed `PutItem`, `GetItem`, and
`Query` but not `UpdateItem`. Least privilege doing its job -- the action was refused
rather than silently permitted. `UpdateItem` is now granted; **`DeleteItem` deliberately
still is not**, because a governance system whose own role can erase its evidence offers
much weaker assurance than one that cannot. A test now asserts the role has `UpdateItem`,
lacks `DeleteItem`, and holds no wildcard DynamoDB action.

---

## 3.12 Milestone 4 — DEPLOYED AND VERIFIED

Simulation harness, dry-run/shadow modes, and a full policy lifecycle. The theme of M4
is **proof**: every earlier milestone claimed the policy behaves a certain way; M4 makes
that claim executable, and makes changing the policy a controlled operation rather than
a redeploy.

### Verified live (base URL above, 19 Aug 2026)

| # | Check | Result |
|---|---|---|
| 1 | `guardrail-sim run scenarios/ --endpoint <live>` | **16/16 passed** in 5.5 s |
| 2 | Agent key attempts `POST /v1/policies` | **403** — "may not change policy" |
| 3 | Admin key publishes v1 (threshold 100 → 10000) | `201`, `activated: false` |
| 4 | Same delete of 500 rows, before activation | still `block` — publishing is inert |
| 5 | Activate v1 | `rollforward`, no redeploy |
| 6 | Same delete of 500 rows, after activation | **`allow`** — hot reload works |
| 7 | Publish v2 (strict baseline), activate | back to `block`, `bundle_version: 2` |
| 8 | **Rollback** to v1 | `direction: rollback`, decision returns to `allow` |
| 9 | Roll forward to v2 | `block` — final state is the strict policy |
| 10 | `GET /v1/policies` | both versions listed, v2 active, nothing deleted |
| 11 | Dry-run parity, live | **16/16 identical decisions**, nothing executed |
| 12 | Change-impact diff vs a loosened candidate | **5 actions flagged LOOSER**, with rules |
| 13 | 5 × `POST /v1/simulate` | audit head unchanged at seq 73 — **no trace** |
| 14 | One real `POST /v1/evaluate` | seq 74 — the chain still advances |
| 15 | `GET /v1/audit/verify` | `chain_valid: true`, 74 records |
| 16 | Audit records after activation | pinned to `bundle default v2` |

Cost gate re-run: **15 WCU / 15 RCU of the free 25, one table, two indexes — unchanged.**
Policy versions share the audit table in their own partitions and add no capacity.

### What was built

**`packages/guardrail-sim/`** — a new workspace package, ~1,100 lines.

* `scenarios.py` — the YAML scenario DSL. Every model is `extra="forbid"`, so a typo
  like `expects:` is an error rather than a scenario with no assertions.
* `runner.py` — `OfflineTarget` (in-process, no AWS) and `LiveTarget` (HTTPS) behind one
  interface, plus `check()` and dry-run parity.
* `diffing.py` — change-impact analysis, including `SimulateTarget` which diffs against
  whatever policy is *actually live* rather than a file someone believes is live.
* `report.py` — console, JUnit XML, self-contained HTML, and JSON renderings.
* `cli.py` — `validate`, `run`, `parity`, `diff`. Non-zero exit on failure.

**`scenarios/`** — 16 scenarios in two suites. `success-criteria.yaml` holds the five
outcomes PS-3.1 names, all marked `critical`. `policy-model.yaml` covers the design
claims: fail-closed UNKNOWN handling, most-restrictive resolution, bcc/backslash/id-list
bypasses, and float thresholds.

**Service** — `storage/policies.py` (immutable versioned bundles + an active pointer),
`policy_provider.py` (per-container cache with timed refresh), `routers/policies.py`
(validate / publish / activate / rollback), `routers/simulate.py`.

**Auth** — `require_policy_admin`, a second privilege separate from holding a valid key.

### Design decisions

**Publishing and activating are separate.** Publishing is inert, so CI can publish on
every merge; activating is the deliberate act that changes behaviour. Rollback is
`activate` with a lower number — deliberately *not* a separate code path, because a
distinct rollback route runs only during incidents, which is the worst possible test
coverage profile for the operation you most need to work.

**The store assigns version numbers, never the file.** Two authors both typing
`version: 2` would otherwise overwrite each other, and `bundle_version` in every audit
record would stop identifying a specific policy.

**Nothing is ever deleted.** The Lambda role still has no `DeleteItem`, so the version
history is append-only *by permission*, not by convention.

**Hot reload is a timed re-check on the request path**, not a poller. Lambda freezes
containers between invocations, so a background thread would not run. 30 seconds is an
explicit, tunable bound on staleness rather than a guess.

**A policy-store outage is not an agent outage.** The provider keeps serving the last
bundle it successfully read, indefinitely. The one ambiguous case — a cold start with an
unreachable store — falls back to the packaged bundle, logs at error level, and reports
`degraded` through `/readyz`. If the published policy was stricter, that is a temporary
loosening; it is marked rather than silent. Documented at length in `policy_provider.py`
because it is the decision most likely to be changed by someone who has not thought it
through.

**Simulation is never audited.** `/v1/evaluate` records what an agent *attempted*; a
simulation is not an attempt. Recording thousands of what-ifs beside real decisions would
dilute the one log meant to answer "what did this agent do". Simulations are still
structured-logged, so the activity stays visible in CloudWatch.

**Policy administration fails closed.** `GUARDRAIL_POLICY_ADMIN_KEY_IDS` defaults to
empty, so nobody can publish until an operator names someone. The alternative default —
any authenticated caller may rewrite policy — is insecure the moment one agent key leaks,
and insecure silently.

### Gaps closed that were not in the M4 plan

**`/readyz` could not fail.** It reported three placeholder table names as unconditionally
ready, so it would have returned 200 through any outage. It now checks the audit table
configuration and the active-policy state, and returns 503 when the policy store is
unreachable. `test_readyz_reports_503_when_the_policy_store_is_unreachable` proves the
probe is load-bearing.

**`matches_active` could never be true.** The first implementation compared a raw
submitted document against a normalised Pydantic dump, which differ in defaults and in
version number — so the field would have read `false` forever while looking meaningful.
Replaced with `semantic_hash`, which compares what a bundle *means*.

**The API key table could be silently mangled.** See the deploy note in §0.

**A stack test read the developer's shell.** `test_policy_administration_is_closed_by_default` asserted the synthesized template carries an empty policy-admin allowlist, but
`ServiceStack` reads `GUARDRAIL_POLICY_ADMIN_KEY_IDS` from `os.environ` at synth time.
So the test passed in CI and on a clean shell, and **failed the moment `.env` was
sourced** — which is the normal state while working on a deploy. Found by running the
documentation cross-check with `.env` loaded. A test whose result moves with ambient
state is untrustworthy in both directions: it can invent a failure, and it can mask a
real regression when the environment happens to agree with the assertion. `_synth` now
clears every variable the stack reads and accepts explicit `overrides`, an autouse
fixture restores the process environment afterwards, and a second test pins the other
half of the contract — that a configured allowlist actually reaches the deployment,
which asserting only the empty default would not have caught.

**The CloudWatch metric budget was miscounted, and M4 filled it.** The plan's arithmetic
counted `dry_run.decisions` as one billed series; it carries an `outcome` dimension, so it
is four. It also reserved slots for `fail_closed_events`, `hitl.queue_age_seconds`, and
`errors`, none of which any code path emits. The real position, confirmed against live
CloudWatch: `decisions` x4 + `dry_run.decisions` x4 + `evaluate.latency_ms` +
`policy.version` = **exactly 10 of the free 10**. M4's `policy.version` took the last slot,
so there is now zero headroom, and implementing the three reserved names as written would
reach 13 and start billing. `observability.py::METRIC_CARDINALITY` now states the real
arithmetic, and `test_metric_cardinality_stays_inside_the_free_tier` asserts it against
the series the service actually emits — the first version of that test used `capsys`,
captured nothing, and passed vacuously, so it also carries a not-empty guard and cases
that map several tools onto one outcome (with one tool per outcome, an added `tool`
dimension would not raise the count and the test would wave through the mistake it exists
to catch).

### Tests

**315 total, up from 218 at the close of M3** — 98 new test functions across three new
files and four existing suites. (pytest's collected count differs slightly from the
function count because a few suites are parametrized.)

* `test_sim_harness.py` (32) — the load-bearing one is
  `test_the_suite_actually_fails_when_policy_regresses`: the suite is run against a
  policy with `db-bulk-delete` deleted and *must* go red. A conformance gate that has
  never been observed failing is not a gate. A second test does the subtler version —
  quietly raising a threshold rather than deleting the rule.
* `test_policy_lifecycle.py` (28) — version assignment, rollback, append-only history,
  float round-tripping, and every branch of the reload/degradation path.
* `test_policy_api.py` (31) — the privilege boundary
  (`test_an_agent_key_cannot_publish_policy`) and the M4 deliverable end to end
  (`test_activating_a_new_version_changes_live_behaviour_without_a_redeploy`).
* `test_service_stack.py` (+3) — policy administration closed by default, a configured
  allowlist actually reaching the deployment, and capacity still 15/15 with exactly one
  table and two indexes.
* `test_api_evaluate.py` (+3) — the CloudWatch metric budget, asserted against the series
  the service actually emits, plus a guard proving the capture is not empty.
* `test_health.py` (+1) — `/readyz` driven to a real 503 with an unreachable policy store.

### CI

The `quality` job now runs `guardrail-sim validate`, the offline conformance suite, and
the dry-run parity check — so a policy regression fails a **pull request**, before
anything is deployed. The `deploy` job runs the same scenarios against the live dev URL
afterwards. Both upload JUnit XML and the HTML evidence report as artifacts.

### Deliberately not done in M4

* **Console policy view.** The review console still shows only the HITL queue. Publishing
  and rollback are API-only. Not required by the M4 exit criteria; noted for M5.
* **Per-tenant policy bundle ids.** Storage and the provider are already tenant-keyed, but
  one deployment serves one `bundle_id`. Full multi-tenancy is M5.
* **Signed policy bundles.** Publication is attributed (`published_by`) but not
  cryptographically signed. Worth doing for a real product; out of scope here.

---

## 3.13 Milestone 5 — DEPLOYED AND VERIFIED

Production hardening and integration breadth. Three things came out of M5 that were not
in the plan, and all three were found by *doing* rather than reviewing: a real throttling
defect, a Dockerfile that had never worked, and an AWS account quota that made part of the
plan impossible.

### Verified live (19 Aug 2026)

| # | Check | Result |
|---|---|---|
| 1 | **Unmodified MCP server governed end to end** | `secure-filesystem-server 0.2.0`, 14 tools, fetched by `npx` |
| 2 | `initialize` / `tools/list` through the proxy | passed through untouched |
| 3 | Ordinary read via the proxy | allowed, real content returned |
| 4 | Private-key read via the proxy | **blocked**, naming `mcp-credential-read-block` |
| 5 | **Canary never appeared in the transcript** | the upstream server never saw the request |
| 6 | Write via the proxy | held for review, file untouched on disk |
| 7 | Conformance against live AWS | **20/20** |
| 8 | Conformance against a plain container | **20/20**, identical |
| 9 | Sustained load, 120 s | **6.1 req/s, 0 errors, 0 throttles** |
| 10 | Engine latency | p50 **4.3 ms**, p99 **11 ms** |
| 11 | Round-trip latency | p50 314 ms, p99 349 ms |
| 12 | Beyond capacity | clean 503 + `Retry-After`, **0 Lambda platform errors** |
| 13 | Alarms deployed | 7 of the free 10, all notifying |
| 14 | Dashboard deployed | 1 of the free 3 |
| 15 | Tenant isolation | another tenant cannot read, resolve, or influence anything |
| 16 | AWS spend | **$0.00** |

Capacity unchanged: **15 WCU / 15 RCU of the free 25**, one table, two indexes.
CloudWatch: **10 of 10** custom metrics, **7 of 10** alarms, **1 of 3** dashboards.

### What was built

**MCP guardrail proxy** (`guardrail_sdk/mcp.py`) — proxies any MCP server over stdio and
evaluates every `tools/call` before forwarding. Everything else passes through untouched,
so the proxy does not become a compatibility hazard as the protocol moves. A refusal is a
result carrying `isError`, not a JSON-RPC error: MCP clients surface the former to the
model as readable content and the latter as a broken server. The upstream runs as the
proxy's own child, so the proxy is the only path to it.

**Framework adapters** (`guardrail_sdk/adapters.py`) — `GuardedToolDispatcher` for any
OpenAI-style tool loop, and `guard_langchain_tool` which wraps a tool's own callable.

**Rate limiting** (`ratelimit.py`) — per-tenant token buckets, in-process.

**Observability** — 7 alarms and 1 dashboard, all reading metrics that already exist.

**Docs** — `docs/threat-model.md`, `docs/policy-authoring.md`, `docs/decisions.md` (18 ADRs).

### Three plan items that turned out to be wrong or impossible

**The DynamoDB token-bucket rate limiter was the wrong design here.** It costs one write
per request; at 5 provisioned WCU that caps the service near 5 req/s and spends the very
capacity the audit chain needs. Built in-process instead. The weakening is real and named:
with N containers the true bound is `N x per-container rate`, `global_ceiling()` returns
that number, and it is **not** a fair-share mechanism.

**Reserved concurrency cannot be set on this account at all.** A new AWS account has a
total `ConcurrentExecutions` quota of 10, and AWS rejects any reservation leaving fewer
than 10 unreserved — so the maximum reservable value is exactly zero. The first deploy
attempt failed on precisely that. The ceiling still exists, enforced by the account quota.
The setting is honoured when present, so raising the quota is a support request, not a code
change.

**A `prod` stage cannot coexist with `dev` on the free tier.** A second stage at the same
shape needs another 15 WCU / 15 RCU, taking the account to 30/30 against a free 25. The
stack is stage-parameterised and hardened, but deploying `prod` alongside `dev` would bill.

### Bugs found by doing rather than reviewing

**DynamoDB write throttling was unhandled.** `append` retried only
`ConditionalCheckFailedException`; everything else re-raised on the first attempt. At 5
provisioned WCU, `ProvisionedThroughputExceededException` is the most likely failure the
repository will ever see, and it escaped as a raw `ClientError` — surfacing as an unhandled
**500** rather than the fail-closed **503** the SDK and the docs both promise, with **no
log line at all** to explain it, and burning most of the 10-second function timeout. A
180-second load test produced 92 Lambda platform errors. Review, 375 unit tests, and the
conformance suite had all missed it.

Fixed with distinct jittered backoff for throttling, a 5-second deadline inside the
function timeout, and botocore's own retries capped so they cannot compound. Afterwards:
**0 Lambda platform errors**, max latency 11.9 s → 7.1 s, `audit_write_failed` logged at
ERROR, `Retry-After` returned, ~3x more requests completed.

**A 30-second load test lied.** It reported 18 req/s with zero errors — DynamoDB burst
credit, not capacity. Publishing that would have described a system that does not exist.
Only a 180-second run reaches the real ceiling.

**The Docker image had never been built.** Portability was asserted from M0. When the image
was finally built in M5 it did not start: `uv` installed `guardrail-core` as an editable,
so `site-packages` carried a `.pth` pointing at a build-stage path absent at runtime, and
the container died at import. Fixed with `--no-editable`; now proved by
`scripts/portability_proof.py`, which CI runs.

**The two policy files could drift silently.** CI validated `policies/default.yaml` while
the Lambda bundled a separate copy. Editing one and not the other would have left the
conformance suite green while production behaved differently. Now asserted byte-identical.

**A rate-limit header crashed the service when limiting was disabled.** `remaining` is
infinity and `int(inf)` raises `OverflowError`, turning every request into a 500 the moment
the limiter was switched off.

**A stack test read the developer's shell.** It asserted an empty policy-admin allowlist,
but the stack reads that variable from `os.environ` — so it passed in CI and failed the
moment `.env` was sourced. `_synth` now clears what it reads and takes explicit overrides.

### Tests

**391, up from 315.** The additions:

* `test_integrations.py` (25) — the MCP proxy and adapters. The load-bearing one is
  `test_a_blocked_call_never_reaches_the_upstream_server`: a proxy that forwarded first and
  logged after would still report `block` and still pass every other test in the file.
* `test_chaos.py` (16) — fail-closed under five real outages, across four call paths,
  asserting no side effect occurred each time. Also that the circuit breaker fails *fast*
  rather than *open* — if it short-circuited to allow, a brief outage would become a total
  governance bypass.
* `test_ratelimit_and_tenancy.py` (21) — the limiter, and the tenant boundary. The one that
  matters most is that another tenant cannot approve your held decision.
* `test_audit_chain.py` (+6) — the throttling regression, including that the retry budget
  finishes inside the Lambda timeout and that a permanent error is *not* retried.
* `test_service_stack.py` (+6) — alarm and dashboard budgets, every alarm notifying, none
  firing on missing data, and reserved concurrency both ways.

### Deliberately not done

* **S3 audit archive** — S3's free tier is 12 months; DynamoDB storage is always-free and
  bundles are kilobytes.
* **X-Ray** — not always-free. Structured logs plus Logs Insights answer the same questions.
* **Cognito console sign-in** — genuinely free but substantial; the console still uses an
  API key in `sessionStorage`. Recorded in the threat model as a gap.
### The AWS-hosted agent gap — CLOSED

The rubric rewards governing agents **also hosted on AWS**, and through M5 the control plane
was deployed while the agent ran on a laptop against Ollama. `Guardrail-Agent-dev` closes it:
a second Lambda with its own Function URL, running the same agent against **Groq**, governed
over public HTTPS by the control-plane Lambda.

Groq rather than the cloudflared tunnel that was scoped in M2. The tunnel works, but the demo
then only runs while one particular machine is awake, and it publishes an unauthenticated
Ollama to the internet. Neither path needed a code change — the provider layer has been
environment-configured since M2, and Ollama stays the default locally.

Verified live, all four outcomes from AWS:

| Task | Outcome | Evidence |
|---|---|---|
| Delete 500 inactive accounts | **blocked** | `db-bulk-delete`, side-effect ledger empty, audit seq 3353 |
| Email an external vendor | **held for review** | `external-email-review`, then approved by a human, seq 3354 |
| Email an internal colleague | **allowed** | executed, seq 3355 |
| Read a confidential path | **log_and_allow** | `confidential-read-audit`, seq 3356 |

Each ran in 3–7 seconds — Groq is far faster than local Ollama, which also makes the demo
practical to run repeatedly. All four appear in the guardrail's own audit log under
`agent_id: aws-ops-assistant`, and the chain still verifies.

Design notes worth keeping: it is a **separate stack** (a broken demo build must never
endanger the control plane, and `cdk destroy` on it cannot touch the audit table); it has
**no IAM grants at all** and reaches the guardrail over HTTPS with an API key exactly as any
third-party agent would; and its endpoint **requires its own key**, because each invocation
spends Groq quota.

Two things found while building it. `qwen/qwen3-32b`, which the plan named, **has been
retired by Groq** — checking `/v1/models` before deploying saved a cycle; the default is now
`qwen/qwen3.6-27b`. And the handler must **reset the side-effect ledger per invocation**,
because it is a module-level list and Lambda reuses warm containers — without that, the
second run would report the first one's actions as its own, which in a system whose output
is evidence would be an outright false transcript.

---

## 3.14 Post-M5 — the self-approval defect

Found while designing authentication for a hosted frontend, not while looking for bugs.

### The defect

`/v1/decisions/{id}/resolve` required only a **valid API key** — not a particular one. So
any key could approve any of its own tenant's held actions, **including the agent's own
key**.

`require_hitl` means "pause for a human". In practice it meant "pause for anyone holding a
key", and the agent being paused held one.

Verified against the live deployment before fixing:

```
agent's action held: 7e765be3
approving with the AGENT'S OWN key (acme-sim)...
>>> SELF-APPROVED. status = approved by "the-agent-itself"
```

The identical argument was already written down for policy administration — *"an agent
whose key can rewrite the policy governing it is not governed"* — and was simply never
extended to approval. Reasoning about one privilege correctly does not generalise on its
own, and `docs/threat-model.md` had asserted a protection that did not exist.

### The fix: roles

| Role | evaluate · simulate · read | resolve | publish policy |
|---|---|---|---|
| `agent` *(default)* | yes | **no** | **no** |
| `reviewer` | yes | yes | **no** |
| `admin` | yes | yes | yes |

Ordered, so a higher role includes everything below it. **The default is `agent`** — a key
table entry that forgets to state a role gets the least privilege, and an unrecognised role
is treated as `agent` so a typo restricts rather than escalates.

`reviewer` is deliberately distinct from `admin`: the person handling day-to-day approvals
is usually not the person allowed to rewrite the rules, and collapsing them would force
every reviewer to hold the highest privilege in the system.

`GUARDRAIL_POLICY_ADMIN_KEY_IDS` is kept as an operational break-glass — it can grant admin
without reissuing a key, and existing deployments rely on it.

### Deployed roles

| Key | Role | Why |
|---|---|---|
| `acme-7b6d7d20` | `reviewer` | The M3 console; approving is its job |
| `acme-sim` | `agent` | Conformance harness **and** the AWS-hosted agent |
| `acme-policy-admin` | `admin` | Policy changes |

### Verified live after the fix

Re-running the identical attack: **403**, decision still `pending`,
`allows_execution: false`. An `admin` key then approved it normally, and the chain still
verifies. The agent still evaluates, simulates, and reads — it simply cannot approve.

### A test that would have passed for the wrong reason

`test_another_tenant_cannot_resolve_a_held_decision` asserts tenant B cannot resolve tenant
A's decision. After the role change, tenant B's key would have been refused by the **role**
check before tenancy was ever consulted — so the test would have kept passing while proving
nothing about isolation. Both tenants in that fixture are now `reviewer`, so the 404 can
only come from tenant isolation itself.

**413 tests**, up from 400.

---

## 4. Decisions made in M0, and why

These are the non-obvious ones. Each was a real fork with a real reason.

### 4.1 Lambda Function URL instead of API Gateway

**Decision:** CloudFront → Lambda Function URL (IAM auth + Origin Access Control).

**Why:** API Gateway's 1M requests/month is a **12-month introductory offer**, not
always-free; afterwards HTTP APIs cost $1.00/million. A Function URL has **no
per-request charge, ever**.

The usual objection to Function URLs is that they expose a public endpoint with no edge
in front. That is solved here: the URL uses `AWS_IAM` auth and CloudFront's OAC signs
requests with SigV4, so the raw `*.lambda-url.*` address returns **403 to everyone except
CloudFront**. This is a *tighter* posture than a public API Gateway stage.

**Cost of the decision:** API Gateway's managed features move into application code —
JWT validation (M3), API keys and rate limiting (M5), request validation (already
Pydantic). Roughly 30–60 lines each, and arguably better owned in code for a governance
product, where those paths should be tested and auditable.

### 4.2 DynamoDB must be provisioned, never on-demand

**Decision:** all tables use `BillingMode.PROVISIONED` at 5 WCU / 5 RCU.

**Why:** the DynamoDB free tier covers 25 GB of storage plus **25 WCU / 25 RCU of
provisioned capacity**. On-demand (`PAY_PER_REQUEST`) has **no free tier at all** and
bills from the first request. Because on-demand is the more natural default everywhere
else, this is the single easiest way to accidentally start paying — so the CI gate
rejects it explicitly.

**One** table at 5/5 plus its two global secondary indexes at 5/5 each = 15 WCU /
15 RCU, inside the 25 allowance, with autoscaling **off** so a traffic spike throttles
rather than silently bills. The original plan called for three separate tables; HITL
decisions (M3) and versioned policy bundles (M4) instead share this one table under
their own sort-key prefixes, which is why neither cost any additional capacity.

### 4.3 CloudWatch metric budget capped at 10

**Why:** the CloudWatch free tier is **10 custom metrics total**, and EMF-generated
metrics bill as custom metrics — where **each unique name + dimension combination counts
separately**. So `decisions{outcome=block}` and `decisions{outcome=allow}` are two
metrics, not one.

The budget is fixed in `observability.py::METRIC_CARDINALITY`.

> **Corrected during the M4 audit.** The table originally recorded here was wrong in
> two ways, and both were confirmed against live CloudWatch. `dry_run.decisions`
> carries an `outcome` dimension, so it is **four** billed series, not one. And
> `fail_closed_events`, `hitl.queue_age_seconds`, and `errors` were reserved but are
> emitted by **no code path**. The figures below are what the service actually emits.

| Metric | Billed series | Emitted? |
|---|---|---|
| `decisions` × 4 outcome dimensions | 4 | yes |
| `dry_run.decisions` × 4 outcome dimensions | 4 | yes |
| `evaluate.latency_ms` × stage | 1 | yes |
| `policy.version` × stage | 1 | yes, since M4 |
| **Total** | **10 of the free 10** | **full** |

There is now **zero headroom**: M4's `policy.version` took the last slot. Implementing
`fail_closed_events`, `hitl.queue_age_seconds`, and `errors` as originally written
would reach 13 and start billing. `test_metric_cardinality_stays_inside_the_free_tier`
asserts the ceiling against the series the service actually emits, so this is enforced
rather than remembered.

Anything finer-grained (per-rule, per-tenant, per-agent) is a structured **log** field,
queried through Logs Insights against the 5 GB log allowance. **Any new metric must
displace an existing one.**

### 4.4 Zip + layer, not a container-image Lambda

**Why:** container-image Lambdas require a **private** ECR repo (public ECR cannot back a
Lambda), and private ECR is 500 MB free for 12 months only, then $0.10/GB-month. Our
dependency set is ~60–80 MB, well inside the 250 MB unzipped limit, so the 10 GB image
ceiling buys nothing and would cost money. A smaller artifact also cold-starts faster,
which matters because a guardrail sits in the hot path of *every* tool call an agent makes.

The Dockerfile still exists — for local parity, as the CI test runner, and as proof the
service is not Lambda-locked. See §5.3.

### 4.5 Log group declared in CDK, not left to Lambda

**Why:** Lambda auto-creates log groups with **infinite retention**, the most common way
to drift past the 5 GB CloudWatch Logs allowance. The group is declared explicitly with
`RetentionDays.ONE_WEEK`, and the cost gate fails any log group lacking `RetentionInDays`.

### 4.6 arm64 (Graviton) and 512 MB memory

arm64 is cheaper per GB-second and faster than x86 for this workload, and the Lambda free
tier is denominated in GB-seconds. At 512 MB, the 400,000 GB-second monthly allowance
covers roughly **800,000 invocations**.

### 4.7 Tracer is constructed lazily

Powertools' `Tracer` imports `aws_xray_sdk` at construction time. Building one at module
scope would drag that SDK into the Lambda zip for a service that does not enable X-Ray
until M5 — and even then only at 5% sampling, because **X-Ray is not always-free**. So
`get_tracer()` constructs on first use instead.

### 4.8 `/healthz` and `/readyz` are genuinely different

`/healthz` performs **no I/O**. A liveness probe that touches a database reports healthy
processes as dead during a transient dependency outage and gets them restarted, deepening
the incident. `/readyz` enumerates dependencies individually rather than collapsing to a
boolean, so a failure says *which* dependency is down.

---

## 5. Bugs found and fixed during M0

Recorded because each is a trap that would otherwise recur.

### 5.1 `request_id` was missing from every access log line

The middleware cleared the Powertools log key in a `finally` block that ran **before** the
access-log line was written — stripping the id from the very line it exists to correlate.
Caught by inspecting real server output, not by the tests as originally written.

Fixed by nesting the try blocks so the key is removed only after logging. Two regression
tests now assert the id is present *and* that it does not leak between requests (which
matters because Lambda reuses warm containers).

### 5.2 Unquoted `>=` in the CDK bundling command

`pip install fastapi>=0.115` inside `bash -c` treats `>` as a **shell redirect**: pip
installs an unpinned `fastapi` and writes a junk file named `=0.115`. Every requirement
is now single-quoted.

### 5.3 Duplicate `lambda:InvokeFunctionUrl` permission

`FunctionUrlOrigin.with_origin_access_control()` already emits a correctly-scoped,
partition-aware permission. The hand-written one was redundant *and* worse (it hardcoded
the `aws` partition). Removed, with a comment recording why it must not be re-added.

### 5.4 `cdk.App()` synthesized into a temp directory

When run as `python app.py` rather than through the CDK CLI, aws-cdk-lib writes to a
temporary directory. The CI cost gate would then have found no templates and — before the
guard was added — could have reported a false pass. Now `outdir` is pinned explicitly, and
the scanner **exits 2 on an empty or missing directory** rather than passing.

### 5.6 Bundling failure masked by `|| true` (found during the first real deploy)

The layer build was expressed as a list of commands joined with ` && ` and ending
`|| true`. Because `&&` and `||` are left-associative with equal precedence, a failure
in the **first** command fell straight through to the trailing `|| true`, and the script
exited **0 with an empty output directory**. CDK then reported the opaque
`BundlingProducedNoOutput`, naming the symptom rather than the cause.

The masked failure was `pip install --upgrade pip`, which dies with
`Permission denied: '/.local'` because CDK runs the bundling container as **uid 1000**.
That step was never necessary; the image's pip is fine as shipped.

Fixed by rewriting both bundling steps as real scripts with `set -euo pipefail`, keeping
`|| true` only on the cleanup lines where it belongs, and adding an explicit emptiness
assertion so the error names the real problem. Verified inside the container before
redeploying: the layer is 14 MB and yields
`_pydantic_core.cpython-312-aarch64-linux-gnu.so` -- genuinely arm64, which is the whole
reason for building inside the Lambda image.

**General lesson:** a build script that cannot fail is not a passing build script.

### 5.7 `cdk.json` used the system Python

`"app": "python app.py"` resolves to whatever `python` comes first on PATH -- the system
interpreter, which has no `aws_cdk` installed -- so `cdk bootstrap` failed with
`ModuleNotFoundError`. Now `"app": "uv run python app.py"`, which always resolves the
workspace venv. This would have broken CI identically.

### 5.5 Test that would have passed either way

The first version of the log-correlation test read stdout, but pytest's logging plugin
intercepts records before Powertools' JSON formatter runs, so it saw plain text and could
never have caught bug 5.1. Rewritten to attach a handler using the **real registered
formatter**, so it tests the format production actually emits.

---

## 6. Architecture as built (M0)

```
Reviewer / Agent
      │  HTTPS
      ▼
┌──────────────────────────┐
│  CloudFront distribution │  PRICE_CLASS_100, caching disabled (a stale
│  (always-free 1 TB/mo)   │  policy decision is a wrong one)
└────────────┬─────────────┘
             │ OAC signs with SigV4
             ▼
┌──────────────────────────┐
│  Lambda Function URL     │  AWS_IAM auth → 403 for anyone but CloudFront
└────────────┬─────────────┘
             ▼
┌──────────────────────────┐
│  Lambda (arm64, 512 MB)  │  Mangum → FastAPI → guardrail_core
│  + dependency layer      │  log group: 7-day retention
└──────────────────────────┘

Deployed separately, account-wide:
  AWS::Budgets::Budget — $1 limit, alerts at 1% actual and 50% forecast
```

Synthesized resources (8 total, all free): `Logs::LogGroup`, `Lambda::LayerVersion`,
`IAM::Role`, `Lambda::Function`, `Lambda::Url`, `Lambda::Permission`,
`CloudFront::OriginAccessControl`, `CloudFront::Distribution`.

---

## 7. WHAT YOU NEED TO PROVIDE — Milestone 0

Nothing here can be generated from code. Ordered by when it is needed.

### 7.1 Before deploying — AWS account setup (one time)

| # | What | Notes |
|---|---|---|
| 1 | **AWS account** on the **Free plan** | Signup requires a payment method even on the Free plan; the Free plan caps you at signup credits and *pauses* rather than bills. |
| 2 | **MFA on the root user** | Then stop using root entirely. |
| 3 | **An IAM admin user or Identity Center user** | Day-to-day use. Root is for billing settings only. |
| 4 | **Region: `us-east-1`** | Not negotiable — CloudFront-associated resources must live there. |

### 7.2 Local tooling — ALREADY INSTALLED FOR YOU

| Tool | Status | Location |
|---|---|---|
| **AWS CLI v2** (2.36.25) | Installed | `%LOCALAPPDATA%\AWSCLIV2\Amazon\AWSCLIV2` — added to user PATH |
| **AWS CDK CLI** (2.1137.0) | Installed | `%APPDATA%\npm` — already on user PATH |
| **Node.js** (22.14.0) | Already present | |
| **Docker Desktop** | Installed, **daemon not running** | You must start it before deploying |

The AWS CLI was installed via an MSI administrative extract into `%LOCALAPPDATA%`, since a
normal MSI install needs admin rights that this session does not have. It works
identically. **Both CLIs require a freshly opened terminal** to appear on PATH.

Run `uv run python scripts/preflight.py` at any time to see exactly what is still missing.

### 7.2b Original tooling requirements (for reference)

| Tool | Why | Check |
|---|---|---|
| **AWS CLI v2** | Not currently installed on this machine. Needed for credentials and `cdk bootstrap`. | `aws --version` |
| **Node.js 18+** | Already present (v22.14.0). Needed for the CDK CLI. | `node -v` |
| **AWS CDK CLI** | `npm install -g aws-cdk@2` | `cdk --version` |
| **Docker Desktop running** | Required to *bundle* the Lambda zip. Not needed to run tests or synth. | `docker info` |

### 7.3 Values I need from you

Put these in `.env` (copy from `.env.example`; `.env` is git-ignored):

| Variable | Where it comes from | Required? |
|---|---|---|
| `GUARDRAIL_ALERT_EMAIL` | **Your email address.** The zero-spend budget alarm sends here. Without it the budget stack is skipped entirely. | **Yes** — this is the cost tripwire |
| `GUARDRAIL_AWS_REGION` | Leave as `us-east-1` | No (defaults correctly) |
| `GUARDRAIL_STAGE` | Leave as `local` for development | No |

AWS credentials themselves are **not** put in `.env`. Run `aws configure` (or
`aws configure sso`) so the CLI and CDK pick them up from `~/.aws/`. Never paste an access
key into a file in this repository.

### 7.4 GitHub repository secrets (only when you enable CI deploys)

The pipeline uses **OIDC**, so there are no long-lived AWS keys anywhere. You will need:

| Secret | What it is |
|---|---|
| `AWS_DEPLOY_ROLE_ARN` | ARN of an IAM role trusting GitHub's OIDC provider, restricted to your repo. I will generate the exact trust policy and creation commands when you are ready. |
| `GUARDRAIL_ALERT_EMAIL` | Same address as above, for the budget stack in CI. |

Also required: a GitHub **environment named `dev`** (Settings → Environments), which the
deploy job targets. Make the repo **public** so Actions minutes are free.

### 7.5 Deploy commands, once the above exists

```bash
# 1. Confirm credentials work and note the account id
aws sts get-caller-identity

# 2. One-time CDK bootstrap for the account/region
cd infra
cdk bootstrap aws://<ACCOUNT_ID>/us-east-1

# 3. Deploy (Docker Desktop must be running for bundling)
GUARDRAIL_STAGE=dev \
GUARDRAIL_VERSION=$(git rev-parse HEAD) \
GUARDRAIL_ALERT_EMAIL=you@example.com \
cdk deploy --all --outputs-file outputs.json

# 4. Verify — BaseUrl is printed in the CDK outputs
curl https://<BaseUrl>/healthz
curl https://<BaseUrl>/readyz

# 5. Verify the origin is NOT publicly reachable (must return 403)
curl -o /dev/null -w '%{http_code}\n' <FunctionUrl>/healthz
```

Step 5 is a **security assertion, not a formality**: a 200 there means the origin is
exposed and OAC is misconfigured. CI runs the same check on every deploy.

---

## 8. Known gaps carried into M1 *(all closed — resolution noted per item)*

> Kept as written so the milestone log stays a log. Every item below has since been
> resolved; each carries a note saying where.

1. **Never deployed to AWS.** All infrastructure synthesizes and passes the cost gate, but
   no resource has been created. First real deploy is the first item of M1.
   **Closed in M1** — deployed continuously since; see §0 for the live URL.
2. **Docker image unbuilt.** Docker Desktop was not running. `Dockerfile` and
   `docker-compose.yml` are written but unverified; build them before relying on the local
   loop. This also means the **CDK bundling path has not run for real** — only the
   `GUARDRAIL_SKIP_BUNDLING=1` placeholder path has been exercised.
   **Closed in M1** — real Docker bundling runs on every deploy; see §5.6 for the
   failure it surfaced.
3. **`/readyz` reports dependencies as ready while unprovisioned.** Correct for M0 (there
   is nothing to check yet) but it must become a real DynamoDB reachability check plus an
   "active policy bundle loaded" assertion in M1.
   **Closed in M4**, later than planned. It stayed a stub through M1–M3, meaning
   `/readyz` could not return 503 under any circumstance — a probe that cannot fail.
   It now checks the audit table configuration and the active-policy state, and a
   test drives it to 503 with an unreachable policy store.
4. **No authentication on any endpoint.** Intentional for M0 — health endpoints are public
   by design. Real authN/Z lands in M3 (Cognito JWT) and M5 (API keys, rate limiting).
   **Closed in M3**, though differently: hashed API keys arrived earlier than planned
   because the Function URL became the public edge. Cognito moved to M5. M4 added a
   separate policy-admin permission on top.
5. **`guardrail-core` contains only `Effect`.** The engine itself is M1.
   **Closed in M1** — engine, operators, extractors, and bundle validation.

---

## 9. How to pick this up cold *(M0-era; **§0 is the current runbook**)*

> Superseded by §0, which covers the deployed system, the API keys, the conformance
> harness, and the policy lifecycle. What remains below is still correct for getting a
> local checkout running, with the test count brought up to date.

```bash
cd d:\Official\Projects\Guardrial

uv sync --all-extras          # install everything
uv run pytest                 # 391 tests, all should pass
uv run ruff check . && uv run mypy packages

# Run the service locally
uv run uvicorn guardrail_service.app:app --port 8080
curl http://localhost:8080/healthz

# Validate infrastructure without Docker or AWS
cd infra && GUARDRAIL_SKIP_BUNDLING=1 GUARDRAIL_STAGE=dev \
  CDK_DEFAULT_ACCOUNT=000000000000 CDK_DEFAULT_REGION=us-east-1 \
  uv run python app.py
cd .. && uv run python scripts/check_banned_resources.py infra/cdk.out
```

**Read `Project Documentation.md`** for the concepts, the policy model, and the full
request pipeline. This file covers status and decisions; that one covers how the system
works.
