# Guardrail — working notes

Action-layer governance for AI agents (hackathon problem statement PS-3.1). Evaluates a
tool call against declarative policy **before** it executes, returning `allow`,
`log_and_allow`, `require_hitl`, or `block`, with a hash-chained audit record for every
decision.

This file is loaded automatically each session. It holds the things that are expensive to
rediscover. Depth lives in `PROGRESS.md` (status, decisions, bugs) and
`Project Documentation.md` (concepts, pipeline, architecture). Both were git-ignored
originally and are now **tracked** — they were scanned for credentials before being
committed.

---

## Hard constraints — do not violate

1. **$0 AWS spend.** Always-free services only. Enforced by `scripts/check_banned_resources.py`,
   which fails the build on NAT/ALB/Fargate/ECR/WAF/Secrets Manager/on-demand DynamoDB.
2. **DynamoDB must be PROVISIONED.** On-demand has *no* free tier and bills from request
   one. Current usage is **15 WCU / 15 RCU of the free 25** (table 5/5 + two GSIs 5/5).
   Any new index or table must fit the remaining 10/10.
3. **CloudWatch free tier is 10 custom metrics total**, and each name+dimension pair
   counts separately. **The budget is exactly full: 10/10.** `decisions` x4 outcomes,
   `dry_run.decisions` x4 outcomes, `evaluate.latency_ms`, `policy.version`. Declared in
   `observability.py::METRIC_CARDINALITY` and **enforced** by
   `test_metric_cardinality_stays_inside_the_free_tier`, which counts the series the
   service actually emits. Adding any metric or any dimension now costs money -- a new
   one must displace an existing one. Anything finer-grained goes in a structured log and
   is queried via Logs Insights.
   The original plan's arithmetic was wrong: it counted `dry_run.decisions` as 1 (it is 4)
   and reserved slots for `fail_closed_events`, `hitl.queue_age_seconds`, and `errors`,
   which no code path emits. Implementing those three as written would reach 13 and bill.
4. **Free LLM only.** Ollama + `qwen3:latest` locally. Bedrock is banned (pay-per-token).
5. **Every milestone ends deployed and verified against live AWS**, not just tested.
6. **Authorisation is by role on the API key**: `agent` < `reviewer` < `admin`,
   defaulting to **`agent`**. Resolving a held decision needs `reviewer`; publishing
   policy needs `admin`. Before this existed, any valid key could approve — so an
   agent could approve the action its own policy held, verified live. Never let the
   default become anything but the least privilege.
7. **Reserved concurrency cannot be set on this account.** The total
   `ConcurrentExecutions` quota is **10** and AWS requires 10 to stay unreserved, so
   the maximum reservable value is zero. The account quota is the ceiling instead.
   A **`prod` stage also cannot coexist with `dev`**: another 15/15 would take the
   account to 30 WCU against a free 25.
8. **The console is reachable over TLS only, and its origins are an explicit
   allowlist, never `*`.**
   `GUARDRAIL_CONSOLE_ORIGINS` is read by **both** the service and agent stacks and must
   match the `Origin` header verbatim, scheme included — console requests carry
   credentials, and a wildcard origin with credentials is rejected by browsers anyway. An
   *empty* value falls back to localhost rather than deploying an empty allowlist: an
   unset CI secret expands to `""`, which would otherwise ship CORS that blocks
   everything, with nothing at deploy time saying so.
9. **Policy administration fails closed.** `GUARDRAIL_POLICY_ADMIN_KEY_IDS` lists the key
   ids allowed to publish or activate policy and defaults to **empty = nobody**. An agent
   whose key can rewrite the policy governing it is not governed. Never make this
   permissive by default. Currently `acme-policy-admin`.

## Deployment coordinates

- AWS account `182355603382`, region **us-east-1** (not negotiable — CloudFront needs it)
- Live base URL: `https://y5ycfqeeilb24ylgmsse2agl5i0njovv.lambda-url.us-east-1.on.aws`
- AWS-hosted agent: `https://lasoey7wnbaptha27mywweefxm0xspdg.lambda-url.us-east-1.on.aws`
- **Review console (M6):**
  `https://guardrail-console-dev-182355603382.s3.us-east-1.amazonaws.com/index.html`
  **HTTPS only.** S3 website hosting is deliberately *not* enabled — it cannot terminate
  TLS, and this page takes an API key. The `s3-website-…` hostname returns
  `NoSuchWebsiteConfiguration`, and `test_website_hosting_is_not_enabled` fails if it
  ever comes back.
- Function `guardrail-service-dev`, table `guardrail-audit-dev`,
  console bucket `guardrail-console-dev-182355603382`
- **Credentials live in `.env` (git-ignored). Read them from there — never hardcode a key
  into a committed file.**

## Milestones

| | Scope | State |
|---|---|---|
| M0 | Foundation, CI, cost gate, health endpoints | done, deployed |
| M1 | Policy engine, `/v1/evaluate`, hash-chained audit | done, deployed |
| M2 | Enforcement SDK + Qwen3 agent | done, verified |
| M3 | HITL workflow + review console | done, deployed |
| M4 | Simulation harness, dry-run, policy versioning | done, verified live |
| M5 | Hardening, multi-tenancy, MCP proxy, load test | done, verified live |
| M6 | React console on S3 + `/v1/me` + roles in the UI | done, deployed, verified live |
| M7 | Policy Studio, change-impact diff, playground, dry-run, conformance, MCP view | not started |

All six milestones are deployed and verified, and the **AWS-hosted agent gap is
closed**: `Guardrail-Agent-dev` is a second Lambda running the demo agent against
Groq, governed over HTTPS by the control-plane Lambda. Ollama remains the default
for local runs.

Deferred deliberately, recorded in `PROGRESS.md` so they don't resurface as phantom TODOs:
SigV4 signing (obsolete — CloudFront was removed), Bedrock adapter (costs money),
Lambda-hosted agent via cloudflared tunnel (declined), Cognito console sign-in (moved to M5).

---

## Commands

```bash
# Quality gate — all four must pass before any commit
uv run ruff check . && uv run ruff format --check .
uv run mypy packages
uv run pytest                      # 434 tests

# The console has its own gate. Node 22; not covered by pytest.
cd apps/console-ui && npm ci && npm run typecheck && npm test && npm run build

# Deploy. Docker Desktop MUST be running (Lambda bundling needs it).
# .env now holds every deploy variable, so sourcing it is the whole command.
# GUARDRAIL_API_KEYS_JSON MUST be single-quoted in .env -- see the deploy trap below.
cd infra && set -a && . ../.env && set +a && GUARDRAIL_STAGE=dev \
  GUARDRAIL_VERSION=$(git rev-parse HEAD) \
  GUARDRAIL_CONSOLE_ORIGINS="http://localhost:5173,http://127.0.0.1:5173" \
  cdk deploy Guardrail-Service-dev --require-approval never --outputs-file outputs.json

# Cost gate (offline, no Docker needed)
cd infra && GUARDRAIL_SKIP_BUNDLING=1 GUARDRAIL_STAGE=dev \
  CDK_DEFAULT_ACCOUNT=182355603382 CDK_DEFAULT_REGION=us-east-1 uv run python app.py
cd .. && uv run python scripts/check_banned_resources.py infra/cdk.out

# Mint an API key (prints the key once; only its hash is deployed)
uv run python scripts/generate_api_key.py --tenant acme --name "reviewer-one"

# Conformance harness. Offline needs no AWS and no credentials -- this is what gates a PR.
set -a && . ./.env && set +a   # GUARDRAIL_API_KEY, GUARDRAIL_POLICY_ADMIN_API_KEY
BASE=https://y5ycfqeeilb24ylgmsse2agl5i0njovv.lambda-url.us-east-1.on.aws
uv run guardrail-sim run scenarios/ -v
uv run guardrail-sim run scenarios/ --endpoint "$BASE" --api-key "$GUARDRAIL_API_KEY" \
  --junit reports/conformance.xml --html reports/conformance.html
uv run guardrail-sim parity scenarios/ --endpoint "$BASE" --api-key "$GUARDRAIL_API_KEY"
uv run guardrail-sim diff   scenarios/ --endpoint "$BASE" --api-key "$GUARDRAIL_API_KEY" \
  --candidate candidate.yaml

# Policy lifecycle. Rollback is `activate` with a lower version -- there is no separate path.
curl -X POST "$BASE/v1/policies" -H "x-api-key: $GUARDRAIL_POLICY_ADMIN_API_KEY" \
  -H 'content-type: application/json' -d '{"bundle": {...}}'
curl -X POST "$BASE/v1/policies/versions/2/activate" \
  -H "x-api-key: $GUARDRAIL_POLICY_ADMIN_API_KEY"

# Govern an UNMODIFIED third-party MCP server (needs node/npx)
uv run python scripts/mcp_demo.py          # end-to-end proof, incl. a leak canary
uv run guardrail-mcp --server filesystem --endpoint "$BASE" \
  -- npx -y @modelcontextprotocol/server-filesystem /some/dir

# Prove the control plane is not Lambda-locked (needs Docker)
uv run python scripts/portability_proof.py

# Load test. Run for >=120s -- a 30s run only measures DynamoDB burst credit.
uv run python scripts/loadtest.py --endpoint "$BASE" --api-key "$GUARDRAIL_API_KEY" \
  --concurrency 2 --duration 120

# Run the AWS-HOSTED agent -- no laptop involved, works any time
curl -X POST "$AGENT" -H "x-api-key: $GUARDRAIL_AGENT_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"task": "Delete all 500 inactive user accounts from the users table."}'

# Deploy it. Needs GROQ_API_KEY and GUARDRAIL_AGENT_TARGET_URL in .env; the stack
# is skipped entirely when the target URL is absent.
cd infra && set -a && . ../.env && set +a && GUARDRAIL_STAGE=dev \
  GUARDRAIL_VERSION=$(git rev-parse HEAD) \
  cdk deploy Guardrail-Agent-dev --require-approval never

# Run the governed agent LOCALLY (needs `ollama serve`)
uv run python -m demo_agent "delete all 500 inactive user accounts"

# M3 console -- one static HTML file, no build step. Kept as a fallback.
cd apps/console && python -m http.server 5173 --bind 127.0.0.1

# M6 React console, locally. Port 5173 is already in GUARDRAIL_CONSOLE_ORIGINS.
cd apps/console-ui && npm run dev

# Build and deploy the console. The URLs are baked in at BUILD time; the key never is.
cd apps/console-ui && \
  VITE_GUARDRAIL_BASE_URL="$BASE" \
  VITE_GUARDRAIL_AGENT_URL="$AGENT" \
  VITE_GUARDRAIL_VERSION=$(git rev-parse --short HEAD) \
  npm run build
cd ../../infra && set -a && . ../.env && set +a && GUARDRAIL_STAGE=dev \
  npx cdk deploy Guardrail-Console-dev --require-approval never

# Mint a key WITH A ROLE. Without --role it defaults to `agent`, which cannot approve.
uv run python scripts/generate_api_key.py --tenant acme --name "console reviewer" \
  --role reviewer --merge "$GUARDRAIL_API_KEYS_JSON"
```

---

## Traps already hit — do not rediscover these

**Windows / tooling**
- `cdk deploy` dies at shutdown with jsii `ENOTEMPTY` on Node 22. Synth *succeeds*; only
  cleanup fails. Clear `%TEMP%\jsii-kernel-*` and retry.
- **Docker Desktop stops on its own.** A bundling failure is usually that, not a code bug.
  Check `docker info` first.
- Git Bash mangles paths starting with `/` when passed to the AWS CLI (`/aws/lambda/...`).
  Prefix commands with `MSYS_NO_PATHCONV=1`.
- `cdk.json` must run `uv run python app.py`. A bare `python` picks the system interpreter,
  which has no `aws_cdk`.

**Deploy**
- **CI does not deploy, and that is a configuration gap rather than a design choice.**
  The account has no GitHub OIDC provider, so `AWS_DEPLOY_ROLE_ARN` cannot be set and
  every deploy is manual. The branch gate now accepts `master` (it read `main` only, so
  the deploy job had never run), and a `deploy-preflight` job skips the deploy with a
  named notice instead of failing on missing credentials. Add the secret and pushes to
  `master` deploy on their own.
- **Verify a hosted model still exists before deploying against it.** The plan named
  Groq's `qwen/qwen3-32b`; it has been retired. `curl .../v1/models` first. Current
  default is `qwen/qwen3.6-27b` (`openai/gpt-oss-20b` also tool-calls correctly).
- **`GUARDRAIL_API_KEYS_JSON` must be single-quoted in `.env`.** Unquoted, `. .env` strips
  the inner double quotes, the deployed table is unparseable, auth fails closed, and
  **every request returns 401** with nothing in the deploy output explaining why. Cost a
  deploy cycle in M4. `service_stack.py::_api_key_table` now fails the synth and names it.
- Three keys are deployed, now with roles: `acme-7b6d7d20` (console, **reviewer**),
  `acme-sim` (conformance *and* the AWS agent, **agent**), `acme-policy-admin`
  (**admin**). Raw values live in `.env` only — and the console key's raw value was
  never recorded anywhere, by design.

**Console (M6)**
- **Tailwind v4 dropped the `text-[--css-var]` shorthand.** It compiles to *nothing* — no
  error, no warning, just an unstyled element. Colours declared in `@theme` generate real
  utilities (`text-block`, `bg-hitl`), which is what the console uses. `color-mix(...)`
  inside an arbitrary value still works fine. After any styling change, grep the built CSS
  for a class you expect rather than trusting the build's exit code.
- **`GUARDRAIL_API_KEY` in `.env` is `acme-sim`, role `agent` — it CANNOT approve.**
  Connecting the console with it makes the review queue permanently read-only. Use
  `GUARDRAIL_CONSOLE_API_KEY` (`acme-console-reviewer`, role `reviewer`).
- The console must be **built before** `cdk deploy Guardrail-Console-dev`; the stack
  raises rather than publishing an empty bucket, because a console returning 404 reads as
  a *service* outage and sends people to debug the Lambda.
- `index.html` is deployed by a **second** `BucketDeployment` with `no-cache`. Vite
  fingerprints assets but not `index.html`, so a single immutable deployment would keep
  serving the previous bundle to every returning browser.
- The agent Lambda's Function URL needed CORS adding in M6. Without it the browser shows a
  bare network error and there is nothing server-side to explain it — a browser
  deliberately refuses to say whether a request failed on CORS or on the host being down.

**Correctness**
- **DynamoDB rejects Python floats.** Payloads are stored as *canonical JSON strings* so
  the bytes hashed at write are the bytes read back. Converting to Decimal risks a
  non-identical round trip → a **false tamper alarm**, which is worse than a missed one.
- IAM grants are enumerated, not wildcarded. The role has `UpdateItem` but **not
  `DeleteItem`** — a governance system whose role can erase its own evidence is much
  weaker. Keep it that way.
- Qwen3 reasons before answering unless told not to; thinking blew past a 300s timeout on
  a five-tool prompt while `/no_think` answered in 34s. Disabled by default.
- Shell build scripts: never end an `&&` chain with `|| true`. Equal precedence means an
  early failure falls through and the script exits 0 with no output.
- Policy versions share the audit table under `TENANT#<tenant>#POLICY#<id>` partitions,
  so versioning added **zero** capacity — still 15/15 of the free 25. Do not add a table.
- Hot reload re-checks the active pointer on the request path every
  `GUARDRAIL_POLICY_REFRESH_SECONDS` (30). Lambda freezes containers between invocations,
  so a background poller cannot work. A policy-store outage keeps serving the last known
  good bundle rather than failing requests — read the `policy_provider.py` module
  docstring before changing that; the trade-off is deliberate and documented there.

- **DynamoDB write throttling is a first-class retryable failure.** `append` handles
  it with jittered backoff bounded by a 5s deadline *inside* the 10s Lambda timeout,
  and botocore's own retries are capped so they cannot compound. Before this, a
  throttle escaped as a raw `ClientError` -> unhandled 500 with no log line, and the
  invocation was killed by the timeout. Do not widen the deadline past the timeout.
- Rate limiting is **in-process, per container**. The real global bound is
  `containers x per-container rate`. It is not a fair-share mechanism; do not
  describe it as one.

**Testing**
- Tests that inspect FastAPI internals rot. The auth tripwire once walked `app.routes`,
  found nothing (FastAPI wraps included routers in a private container), and **passed
  vacuously while `/v1/evaluate` was exposed**. Prefer behavioural assertions — send a
  real unauthenticated request and expect 401 — and add a meta-test proving the discovery
  isn't empty.
- The same disease appeared twice more in M4: `/readyz` hard-coded `ready: true` for three
  placeholder tables, so it could never return 503; and `matches_active` compared a raw
  document against a normalised model dump, so it could never be true. **A check that
  cannot fail is worse than no check** — it reads as coverage. Both now have a test that
  deliberately breaks the thing and requires the check to notice.
- `test_the_suite_actually_fails_when_policy_regresses` is why the conformance suite is
  worth anything: it deletes a rule and requires the suite to go red. Keep it.
- **Claims that were never executed were all false.** The Dockerfile asserted
  portability from M0 and had never been built -- the image did not start (`uv`
  installed guardrail-core as an editable, leaving a `.pth` pointing at a build-stage
  path). `scripts/portability_proof.py` now runs the real conformance suite against a
  real container, in CI. Prefer executing a claim over asserting it.
- **Load tests shorter than ~120s lie here.** A 30s run reported 18 req/s with zero
  errors; that is DynamoDB burst credit, not capacity. The sustainable figure is
  ~6 req/s. See `reports/loadtest.md`.
- The two policy files (`policies/default.yaml` and the copy bundled into the Lambda)
  are asserted **byte-identical**. CI validates one and the Lambda ships the other.
- A UI that gates on permissions needs a test that it does *not* over-claim **and** one
  that it does not under-claim. Hiding a control from someone who holds the role is the
  quieter bug and, during an incident, the more expensive one.
- **Tests must not read the developer's shell.** `test_service_stack.py` synthesizes the
  CDK stack, which reads `GUARDRAIL_POLICY_ADMIN_KEY_IDS` from `os.environ` — so after
  `. ./.env` the suite failed locally while passing in CI. `_synth` now clears every
  variable the stack reads and takes explicit `overrides`. Run the suite **both** ways
  after touching infra tests: `uv run pytest` and `set -a && . ./.env && set +a &&
  uv run pytest` must agree.

---

## Working style the user expects

- **Audit against the plan before declaring a milestone done.** They ask "is it complete?"
  and want the honest gap list, including scope that turned out obsolete.
- State trade-offs plainly; don't oversell. Flag deviations rather than letting them pass.
- Verify against live AWS, not just tests, before claiming a milestone is finished.
- Comments explain *why*, especially where a choice looks unusual.
