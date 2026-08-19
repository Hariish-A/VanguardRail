# Running and testing Guardrail, end to end

A hands-on walkthrough of everything built across M0–M7: what to run, what you should see,
and — for the console — **which key to paste where and why there is more than one.**

Written to be followed top to bottom by someone who has not run any of it yet.

> **Everything in this document was executed against the live deployment while writing
> it.** Where something was *not* run, it says so.

---

## 0. Read this first — the four credentials

This is the single most confusing thing in the system, and the thing most likely to make a
demo look broken. There are **two separate credential namespaces**, and a key from one is
rejected by the other.

### Namespace 1 — the control plane (the guardrail itself)

Three keys, in `.env`, differing only in **role**. The role decides what the console will
even render.

| `.env` variable | key id | role | Can it approve? | Can it change policy? |
|---|---|---|---|---|
| `GUARDRAIL_API_KEY` | `acme-sim` | `agent` | **no** | **no** |
| `GUARDRAIL_CONSOLE_API_KEY` | `acme-console-reviewer` | `reviewer` | **yes** | no |
| `GUARDRAIL_POLICY_ADMIN_API_KEY` | `acme-policy-admin` | `admin` | yes | **yes** |

Verified live:

```bash
set -a && . ./.env && set +a
BASE=https://y5ycfqeeilb24ylgmsse2agl5i0njovv.lambda-url.us-east-1.on.aws
curl -s "$BASE/v1/me" -H "x-api-key: $GUARDRAIL_CONSOLE_API_KEY"
# {"key_id":"acme-console-reviewer","role":"reviewer","capabilities":[...]}
```

### Namespace 2 — the demo agent Lambda

| `.env` variable | What it authenticates to |
|---|---|
| `GUARDRAIL_AGENT_API_KEY` | **The agent Lambda only.** Not the control plane. |
| `GUARDRAIL_AGENT_GUARDRAIL_KEY` | What the *agent* uses to call the guardrail (it is `acme-sim`, role `agent` — the agent deliberately cannot approve its own held actions) |

The separation is real, not cosmetic. Verified live:

```text
GUARDRAIL_AGENT_API_KEY   → control plane /v1/me   →  401
GUARDRAIL_AGENT_API_KEY   → POST agent             →  200
GUARDRAIL_CONSOLE_API_KEY → POST agent             →  401
```

The agent endpoint is authenticated because every invocation spends Groq quota — an open
endpoint would be a denial-of-wallet.

### So which do I paste into the console?

The Connect screen has **four** fields. Two are for the control plane, two for the agent:

| Field | Value | Notes |
|---|---|---|
| Control plane base URL | `https://y5ycfqeeilb24ylgmsse2agl5i0njovv.lambda-url.us-east-1.on.aws` | Pre-filled in the deployed build |
| **API key** | `$GUARDRAIL_CONSOLE_API_KEY` | **Use the reviewer key.** See below |
| Agent URL | `https://lasoey7wnbaptha27mywweefxm0xspdg.lambda-url.us-east-1.on.aws` | Pre-filled |
| Agent key | `$GUARDRAIL_AGENT_API_KEY` | Different credential. Optional — only the Agent Console needs it |

**Use `GUARDRAIL_CONSOLE_API_KEY`, not `GUARDRAIL_API_KEY`.** `GUARDRAIL_API_KEY` is
`acme-sim` with role `agent`; connect with it and the Review Queue is permanently
read-only. That is *correct* behaviour — an agent that can approve the action its own
policy held has not been governed — but it makes for a dull demo.

To exercise **Policy Studio's** publish and activate buttons, reconnect with
`GUARDRAIL_POLICY_ADMIN_API_KEY`.

**Try all three.** Watching the same page render different controls for different keys is
the fastest way to see the role model working.

### Getting the values out of `.env`

```bash
cd d:/Official/Projects/Guardrial
grep -E '^GUARDRAIL_(CONSOLE_API_KEY|API_KEY|POLICY_ADMIN_API_KEY|AGENT_API_KEY)=' .env
```

`.env` is git-ignored. Only SHA-256 hashes are deployed, so nothing in the repo, the
CloudFormation template, or the Lambda environment discloses a usable credential.

---

## 1. Prerequisites

Already installed and verified on this machine: `uv`, `node` 22, `docker`, `aws`, `cdk`,
`npx`, `ollama` (with `qwen3:latest` pulled — the exact model the local agent defaults to).

```bash
cd d:/Official/Projects/Guardrial
uv sync --all-extras
cd apps/console-ui && npm ci && cd ../..
```

**Docker Desktop stops on its own.** If a `cdk deploy` fails at bundling, run
`docker info` before suspecting the code.

---

## 2. The five-minute check — does any of it work?

No AWS, no credentials, no network:

```bash
uv run pytest                              # 441 tests
uv run guardrail-sim run scenarios/ -v     # 20/20 policy conformance, offline
```

Then against the live deployment:

```bash
set -a && . ./.env && set +a
BASE=https://y5ycfqeeilb24ylgmsse2agl5i0njovv.lambda-url.us-east-1.on.aws
curl -s "$BASE/healthz"
uv run guardrail-sim run scenarios/ --endpoint "$BASE" --api-key "$GUARDRAIL_API_KEY"
```

Expected: `20/20 passed`. If that works, everything below will.

---

## M0 — Foundation, health, cost gate

**What it proves:** the thing is deployed, reports which commit it is running, and no
billable resource can reach AWS.

```bash
BASE=https://y5ycfqeeilb24ylgmsse2agl5i0njovv.lambda-url.us-east-1.on.aws

curl -s "$BASE/healthz"    # liveness — performs no I/O, deliberately
curl -s "$BASE/readyz"     # readiness — 503 if a dependency is down
curl -s "$BASE/version"
```

`/healthz` returns the deployed git SHA. Compare it with `git rev-parse HEAD` — if they
differ, the deployed build is not this checkout.

**The cost gate** (offline, no Docker):

```bash
cd infra && GUARDRAIL_SKIP_BUNDLING=1 GUARDRAIL_STAGE=dev GUARDRAIL_DEPLOY_CONSOLE=1 \
  CDK_DEFAULT_ACCOUNT=182355603382 CDK_DEFAULT_REGION=us-east-1 uv run python app.py
cd .. && uv run python scripts/check_banned_resources.py infra/cdk.out
# OK: 4 template(s) contain no paid resources
```

**Make it fail on purpose** — this is the point of a gate. Add a NAT gateway to a stack and
re-run; the scan should refuse the build.

**Check the actual bill:**

```bash
aws budgets describe-budgets --account-id 182355603382 \
  --query 'Budgets[0].CalculatedSpend.ActualSpend.Amount' --output text
# 0.0
```

---

## M1 — Policy engine, `/v1/evaluate`, hash-chained audit

**What it proves:** the five success criteria from the problem statement, and that the log
is tamper-evident rather than merely append-only.

### The five criteria, by hand

```bash
set -a && . ./.env && set +a
BASE=https://y5ycfqeeilb24ylgmsse2agl5i0njovv.lambda-url.us-east-1.on.aws
K="x-api-key: $GUARDRAIL_API_KEY"

ev() { curl -s -X POST "$BASE/v1/evaluate" -H "$K" -H 'content-type: application/json' -d "$1"; }

# 1. bulk delete → block
ev '{"agent_id":"t","session_id":"s","tool":"db.delete_records","arguments":{"table":"users","count":500}}'
# 2. small delete → allow
ev '{"agent_id":"t","session_id":"s","tool":"db.delete_records","arguments":{"table":"users","count":5}}'
# 3. external email → require_hitl
ev '{"agent_id":"t","session_id":"s","tool":"email.send","arguments":{"to":["x@external.com"]}}'
# 4. internal email → allow
ev '{"agent_id":"t","session_id":"s","tool":"email.send","arguments":{"to":["x@acme-corp.com"]}}'
# 5. confidential read → log_and_allow
ev '{"agent_id":"t","session_id":"s","tool":"file.read","arguments":{"path":"/srv/confidential/q3.pdf"}}'
```

### The part worth doing yourself: try to get past it

```bash
# Hide the recipient in bcc — the extractor reads to, cc AND bcc
ev '{"agent_id":"t","session_id":"s","tool":"email.send","arguments":{"to":["ok@acme-corp.com"],"bcc":"leak@external.com"}}'
# → require_hitl anyway

# Send an id list instead of a count — record_count is derived either way
ev '{"agent_id":"t","session_id":"s","tool":"db.delete_records","arguments":{"table":"users","ids":[1,2,3, ...220 ids]}}'
# → block anyway

# Send something unparseable — UNKNOWN fails CLOSED
ev '{"agent_id":"t","session_id":"s","tool":"db.delete_records","arguments":{"table":"users","where":"gibberish"}}'
# → does not fall through to allow
```

### The audit chain

```bash
curl -s "$BASE/v1/audit?limit=5" -H "$K"
curl -s "$BASE/v1/audit/verify" -H "$K"
# {"chain_valid":true,"records_checked":1000,...}
```

`hash = sha256(prev_hash ‖ canonical_json(payload))`, so record *n* commits to *n−1*. It
detects an edited record, a deleted one (sequence gap), and a reordered one (broken link).
It does **not** detect a consistent rewrite of the whole chain by someone with table-wide
write access — that needs an external anchor, which is not implemented and is gap 1 in the
threat model.

---

## M2 — The SDK and a real LLM agent

**What it proves:** a real model's tool call is intercepted *before* execution — not a
hand-written envelope.

### Local agent, local model (needs `ollama serve`)

```bash
set -a && . ./.env && set +a
export GUARDRAIL_BASE_URL=https://y5ycfqeeilb24ylgmsse2agl5i0njovv.lambda-url.us-east-1.on.aws
uv run python -m demo_agent "delete all 500 inactive user accounts from the users table"
```

Qwen3 chooses `db.delete_records`, the SDK asks the deployed guardrail first, the call is
refused, and the agent explains why. Options: `--dry-run`, `--max-turns`, `--model`.

Observed while writing this, against the live control plane:

```text
TOOL CALLS THE MODEL ATTEMPTED
  [BLOCKED  ] db.delete_records
             args: {'count': 500, 'table': 'users', 'where': "status = 'inactive'"}
             policy: db-bulk-delete, destructive-tool-in-production
             audit seq: 3432

WHAT ACTUALLY HAPPENED (side effects)
  (nothing -- every attempted action was refused or held)

AGENT'S REPLY
  The deletion of 500 records is blocked by the db-bulk-delete policy... To proceed,
  you must narrow the selection to 100 records or fewer.
  1. Split into batches...  2. Refine the query...
```

Three things worth noticing. The model wrote a `WHERE` clause the prompt never mentioned,
and `record_count` was derived from it anyway. The side-effect ledger is **empty** — the
block is checkable, not just claimed. And the agent *reasoned about* the refusal and
offered a compliant alternative, which is what makes the guardrail usable rather than
merely obstructive.

### AWS-hosted agent, hosted model (no laptop involved)

```bash
curl -X POST https://lasoey7wnbaptha27mywweefxm0xspdg.lambda-url.us-east-1.on.aws \
  -H "x-api-key: $GUARDRAIL_AGENT_API_KEY" -H 'content-type: application/json' \
  -d '{"task": "Delete all 500 inactive user accounts, then read /srv/confidential/q3.pdf."}'
```

**Read `side_effects` in the response, not just `tool_calls`.** That is the ledger of what
actually happened. A blocked call must leave *nothing* there — that is the difference
between "the agent says it was blocked" and "nothing was done".

### The fail-closed property

```bash
GUARDRAIL_BASE_URL=https://127.0.0.1:9 uv run python -m demo_agent "delete 500 accounts"
```

With the guardrail unreachable the SDK **blocks** rather than proceeding. An outage stops
governed agents; it does not become a bypass.

---

## M3 — Human-in-the-loop

**What it proves:** `require_hitl` means "pause for a *human*" — and, since the role fix,
actually enforces that.

```bash
# Hold an action
HELD=$(curl -s -X POST "$BASE/v1/evaluate" -H "x-api-key: $GUARDRAIL_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"agent_id":"t","session_id":"s","tool":"email.send","arguments":{"to":["a@external.com"]}}')
DID=$(echo "$HELD" | python -c "import sys,json;print(json.load(sys.stdin)['hitl']['decision_id'])")

# The AGENT key tries to approve its own held action → 403
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$BASE/v1/decisions/$DID/resolve" \
  -H "x-api-key: $GUARDRAIL_API_KEY" -H 'content-type: application/json' \
  -d '{"approve":true,"reason":"self-approval"}'

# The REVIEWER key approves → 200
curl -s -X POST "$BASE/v1/decisions/$DID/resolve" \
  -H "x-api-key: $GUARDRAIL_CONSOLE_API_KEY" -H 'content-type: application/json' \
  -d '{"approve":true,"reason":"checked with finance"}'
```

**This is the most important test in the project.** Before roles existed, any valid key
could approve — and it was demonstrated live, with the audit chain recording
`reviewer: the-agent-itself`.

Also worth exercising: **deny** (`"approve": false`), and **timeout** — leave a held
decision for its `timeout_seconds` and poll `GET /v1/decisions/{id}`; it becomes `expired`
with `allows_execution: false`. Silence must not become consent.

---

## M4 — Simulation, dry-run, policy versioning

```bash
# Offline — this is what gates a pull request
uv run guardrail-sim run scenarios/ -v
uv run guardrail-sim validate scenarios/ --policy policies/default.yaml

# Live
uv run guardrail-sim run scenarios/ --endpoint "$BASE" --api-key "$GUARDRAIL_API_KEY" \
  --junit reports/conformance.xml --html reports/conformance.html

# Dry-run parity: does dry-run report what enforcement really does?
uv run guardrail-sim parity scenarios/ --endpoint "$BASE" --api-key "$GUARDRAIL_API_KEY"

# Change impact against a candidate bundle
uv run guardrail-sim diff scenarios/ --endpoint "$BASE" --api-key "$GUARDRAIL_API_KEY" \
  --candidate candidate.yaml
```

**Prove the suite can fail** — otherwise it is a green badge, not a check. Delete a rule
from `policies/default.yaml` and re-run offline; it must go red. (There is a test that does
exactly this: `test_the_suite_actually_fails_when_policy_regresses`.)

The full **publish → activate → rollback** loop is easier in the console — see Policy
Studio below.

---

## M5 — Hardening, multi-tenancy, MCP proxy

### The MCP proxy — the headline integration

```bash
uv run python scripts/mcp_demo.py
```

Runs the real, unmodified `@modelcontextprotocol/server-filesystem`, asks it for a private
key file, and asserts a canary string inside that file appears **nowhere in the entire
transcript**. That is the difference between "the proxy said no" and "the bytes never left
the disk". An ordinary read still succeeds and a write is held for review — so it also
shows the proxy is not simply refusing everything.

> Not re-run while writing this document. It was verified in M5 and runs in CI.

Front any MCP server yourself:

```bash
uv run guardrail-mcp --server filesystem --endpoint "$BASE" \
  -- npx -y @modelcontextprotocol/server-filesystem /some/dir
```

### Portability — the control plane is not Lambda-locked

```bash
uv run python scripts/portability_proof.py
```

Builds the Docker image and runs the **same** conformance suite against a plain container.
This exists because the Dockerfile asserted portability from M0 and had never been built —
when it finally was, the image did not start.

### Load test

```bash
uv run python scripts/loadtest.py --endpoint "$BASE" --api-key "$GUARDRAIL_API_KEY" \
  --concurrency 2 --duration 120
```

**Run for ≥120 s.** A 30-second run reports ~18 req/s, which is DynamoDB burst credit, not
capacity. The sustainable figure is ~6 req/s. See `reports/loadtest.md`.

### Rate limiting and tenancy

Rate limiting is **off by default** in the deployed dev stage. Tenant isolation is covered
by `tests/unit/test_ratelimit_and_tenancy.py` — one tenant cannot read, resolve, or
influence another's anything, and tenancy comes from the verified key, never the request
body.

---

## M6 & M7 — The console

**Open:** <https://guardrail-console-dev-182355603382.s3.us-east-1.amazonaws.com/index.html>

HTTPS only — the HTTP `s3-website-…` endpoint is deliberately switched off, because this
page takes an API key.

**Run it locally instead** (points at the deployed backend; `localhost:5173` is already in
the CORS allowlist):

```bash
cd apps/console-ui && npm run dev
```

> **Known limitation, verified:** a console at `localhost:5173` **cannot** talk to a
> control plane you run locally on `localhost:8098`. CORS lives on the Lambda Function URL,
> not in the FastAPI app, so a locally-run service sends no CORS headers at all. Run the
> local console against the **deployed** backend, or drive a local backend with `curl`.
> (One-line fix available: CORS middleware in the app for the `local` stage. Not done —
> say the word.)

### Page by page

#### Overview — `#/`

Readable **without a key**. What an action guardrail is, the four outcomes, the pipeline,
and the self-approval defect this project found in itself.

Connect a key and the "Live" strip fills with real figures — chain status, recent
decisions, active policy version.

**Test:** open it with no key. Everything explanatory renders; only the live strip asks you
to connect.

#### Connect — `#/connect`

Paste the four values from §0. The key is verified against `/v1/me` **before** it is
stored.

**Test the failure path:** paste a wrong key. You should get a red `HTTP 401` panel *and*
nothing stored — reload and you are still disconnected. A console that looks connected and
is not is the worst state for this tool.

**Test the role model:** connect as each of the three keys in turn and watch the pages
change. Nothing is hidden that you hold; nothing is offered that you do not.

#### Agent Console — `#/agent`

Needs the **agent** URL and key. Pick a preset — "Mixed — all four in one run" is the best
single demo — and run it. Takes 10–30 s.

**What to look at:** the **side-effect ledger** at the bottom. If the run blocked a delete,
that ledger must not contain it.

#### Decision Theatre — `#/theatre`

Send any tool call. Eight presets, including three that try to *evade* policy.

**Evaluate vs Simulate is the lesson:** Evaluate is the hot path — it writes to the audit
chain and can queue a review. Simulate answers the same question and records nothing.

**Test:** run "Evasion — bcc instead of to" and check it is still held. Then run "Unparseable
— fails closed" and confirm it does not fall through to `allow`. Use **Simulate** to see the
derived facts panel — the hot path leaves them out to keep the payload small.

#### Review Queue — `#/review`

**Connect as the agent key first.** The held action is visible; Approve and Deny are
**absent**, with an explanation. That is the defect this project shipped once, now closed.

**Then reconnect as the reviewer key.** The buttons appear. Approve one with a reason, and
find that reason in the audit chain — "who approved this" is only half the question an
auditor asks.

Watch the countdown: `on_timeout` is `deny`.

#### Audit & Chain — `#/audit`

Every decision, oldest first, each linked to its predecessor by hash. Click a row to expand
arguments, derived facts, matched rules, and the full hash.

**Read the bottom panel.** It lists what the chain does *not* detect. A page that claimed
otherwise would be the kind of overstatement this project argues against.

#### Policy Studio — `#/policy`

**Connect as the admin key** or publish and activate will not render.

The full loop, verified live:

1. Edit the bundle — raise `db-bulk-delete` from 100 to 250.
2. **Validate.** Any key may do this. Note `matches_active` — if it says the draft equals
   what is in force, the file you are editing is not the file you think is deployed.
3. **Publish.** Nothing changes. Check Decision Theatre: a 150-row delete is still blocked.
4. **Activate.** Now a 150-row delete is *allowed* — with **no redeploy**.
5. **Roll back** by activating the previous version. There is no separate rollback path; the
   server reports `direction: rollback`.

Publishing and activating are separate buttons on purpose. One control doing both would
make "save my draft" and "change what every agent may do" the same gesture.

#### Change Impact — `#/impact`

Paste a candidate bundle (or pick a published version) and compare against what is in
force. 20 actions × 2 simulations, recording nothing.

**Look at "would become permitted".** "3 decisions changed" is a fact; "one action that is
blocked today would be allowed" is a decision to make.

**Test:** take the active bundle, raise the delete threshold to 250, and compare. You should
see a LOOSER finding. Then *lower* it to 50 and you should see STRICTER instead.

#### Playground — `#/playground`

Click **Build the rule matrix**. It runs every corpus action against the selected policy and
reports which rules fired — and which fired for **nothing**.

**Test the dead-rule detector:** paste a draft with a rule whose threshold nothing can reach
(`record_count > 999999`). It should appear under "never fired". The page will not tell you
*why* — the corpus may not exercise it, or the predicate may be unsatisfiable — and it says
so rather than accusing.

#### Dry-run & Shadow — `#/dryrun`

Three levels, and they differ in ways that matter. Read the table first.

**The parity run writes to the audit chain** — two records per action — and that is the
point: running parity through `/v1/simulate` would prove nothing about the enforcement
path. Defaults to the five criteria, not the full corpus.

**Test:** run it and watch the audit sequence move. Then click through to the Audit page and
find the records tagged `dry_run`.

#### Conformance — `#/conformance`

Runs the **real** `scenarios/*.yaml` corpus — compiled in at build time so it cannot drift
from CI's — against live AWS. Expect **20/20**.

Filter to "Critical" — those are the problem statement's stated requirements, counted
separately so "18 of 20 passed" cannot hide one.

**Note what the page tells you:** it is *not* the gate. `guardrail-sim` is.

#### MCP Proxy — `#/mcp`

Runs the four MCP scenarios live, which verifies the **policy**. What verifies the proxy
*obeyed* it is the leak canary in `scripts/mcp_demo.py` — and the page separates the two
rather than letting a green tick imply both.

#### System Health — `#/health`

`/healthz`, `/readyz`, the policy in force, and the free-tier ceilings. Two of the four
budgets are **exactly full**, which is why certain things are logs rather than metrics.

---

## Running the whole thing locally, with no AWS

Verified end to end while writing this.

```bash
# 1. Mint a local key
uv run python -c "from guardrail_service import auth; print(auth.hash_key('local-test-key'))"

# 2. Start the service with that hash and the admin role
export GUARDRAIL_STAGE=local
export GUARDRAIL_API_KEYS_JSON='{"<hash-from-step-1>":{"key_id":"local-admin","tenant_id":"local","name":"local dev","role":"admin"}}'
uv run uvicorn guardrail_service.app:app --port 8098
```

Then in another shell:

```bash
B=http://127.0.0.1:8098; K="x-api-key: local-test-key"
curl -s $B/v1/me -H "$K"
curl -s -X POST $B/v1/evaluate -H "$K" -H 'content-type: application/json' \
  -d '{"agent_id":"l","session_id":"s","tool":"db.delete_records","arguments":{"table":"users","count":500}}'
curl -s $B/v1/audit/verify -H "$K"
```

Observed: `block` on the bulk delete, a held decision resolvable with the same key, three
audit records, `chain_valid: true`. **The entire policy engine, HITL loop, and hash chain
work with no AWS account at all** — storage falls back to an in-process implementation.

---

## The full quality gate

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy packages
uv run pytest                                    # 441

cd apps/console-ui && npm run typecheck && npm test && npm run build   # 70
```

Run pytest **both** with and without `.env` sourced — `set -a && . ./.env && set +a && uv
run pytest` must also pass. Infra tests read the environment, and sourcing `.env` once made
the suite fail locally while passing in CI.

---

## Deploying your own changes

```bash
# Docker Desktop must be running.
cd infra && set -a && . ../.env && set +a && GUARDRAIL_STAGE=dev \
  GUARDRAIL_VERSION=$(git rev-parse HEAD) \
  npx cdk deploy Guardrail-Service-dev --require-approval never --outputs-file outputs.json

# Console — build FIRST; the stack refuses to publish an empty bucket
cd apps/console-ui && \
  VITE_GUARDRAIL_BASE_URL="$BASE" VITE_GUARDRAIL_AGENT_URL="$AGENT" \
  VITE_GUARDRAIL_VERSION=$(git rev-parse --short HEAD) npm run build
cd ../../infra && npx cdk deploy Guardrail-Console-dev --require-approval never
```

**CI does not deploy.** The account has no GitHub OIDC provider, so `AWS_DEPLOY_ROLE_ARN`
cannot be set; the `deploy-preflight` job reports *Deploy skipped* with a notice rather than
failing. Add the secret and pushes to `master` deploy on their own.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Every request returns **401** | Wrong key, or `GUARDRAIL_API_KEYS_JSON` was unquoted in `.env` — the shell strips the inner quotes and the deployed table is unparseable |
| Console shows a bare network error | CORS. The origin must be in `GUARDRAIL_CONSOLE_ORIGINS` **verbatim, scheme included**. A browser refuses to say whether it was CORS or a dead host |
| Approve/Deny missing in Review Queue | Connected with an `agent`-role key. Use `GUARDRAIL_CONSOLE_API_KEY` |
| Publish/Activate missing in Policy Studio | Needs `admin`. Use `GUARDRAIL_POLICY_ADMIN_API_KEY` |
| Agent Console: 401 | The agent has its **own** key — `GUARDRAIL_AGENT_API_KEY`, not the control-plane key |
| `cdk deploy` fails at bundling | Docker Desktop stopped. `docker info` |
| `cdk deploy` dies with jsii `ENOTEMPTY` | Node 22 cleanup bug. The synth succeeded — clear `%TEMP%\jsii-kernel-*` and retry |
| Console shows an old build | Hard-refresh. `index.html` is served `no-cache`, but a proxy may not honour it |
| Local console cannot reach local service | Expected — see the M6/M7 note above |

---

## What is deliberately not covered

* **SigV4 signing** — obsolete once CloudFront was removed.
* **Bedrock adapter** — pay-per-token, and the project is pinned to $0.
* **Cognito console sign-in** — scoped, deferred; the console uses a pasted API key in
  `sessionStorage`. An XSS on that page would read it. See `docs/threat-model.md`, gap 6.
* **External anchoring of the audit chain** — gap 1, and the honest limit on tamper
  evidence.
