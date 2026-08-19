# Guardrail — working notes

Action-layer governance for AI agents (hackathon problem statement PS-3.1). Evaluates a
tool call against declarative policy **before** it executes, returning `allow`,
`log_and_allow`, `require_hitl`, or `block`, with a hash-chained audit record for every
decision.

This file is loaded automatically each session. It holds the things that are expensive to
rediscover. Depth lives in `PROGRESS.md` (status, decisions, bugs) and
`Project Documentation.md` (concepts, pipeline, architecture) — both git-ignored by the
user's choice, both still on disk.

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
6. **Policy administration fails closed.** `GUARDRAIL_POLICY_ADMIN_KEY_IDS` lists the key
   ids allowed to publish or activate policy and defaults to **empty = nobody**. An agent
   whose key can rewrite the policy governing it is not governed. Never make this
   permissive by default. Currently `acme-policy-admin`.

## Deployment coordinates

- AWS account `182355603382`, region **us-east-1** (not negotiable — CloudFront needs it)
- Live base URL: `https://y5ycfqeeilb24ylgmsse2agl5i0njovv.lambda-url.us-east-1.on.aws`
- Function `guardrail-service-dev`, table `guardrail-audit-dev`
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
| M5 | Hardening, multi-tenancy, MCP proxy, load test | **next** |

Deferred deliberately, recorded in `PROGRESS.md` so they don't resurface as phantom TODOs:
SigV4 signing (obsolete — CloudFront was removed), Bedrock adapter (costs money),
Lambda-hosted agent via cloudflared tunnel (declined), Cognito console sign-in (moved to M5).

---

## Commands

```bash
# Quality gate — all four must pass before any commit
uv run ruff check . && uv run ruff format --check .
uv run mypy packages
uv run pytest                      # 311 tests

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

# Run the governed agent (needs `ollama serve`)
uv run python -m demo_agent "delete all 500 inactive user accounts"

# Review console
cd apps/console && python -m http.server 5173 --bind 127.0.0.1
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
- **`GUARDRAIL_API_KEYS_JSON` must be single-quoted in `.env`.** Unquoted, `. .env` strips
  the inner double quotes, the deployed table is unparseable, auth fails closed, and
  **every request returns 401** with nothing in the deploy output explaining why. Cost a
  deploy cycle in M4. `service_stack.py::_api_key_table` now fails the synth and names it.
- Three keys are deployed: `acme-7b6d7d20` (M3 console), `acme-sim` (conformance,
  evaluate-only), `acme-policy-admin` (policy changes). Raw values live in `.env` only.

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

---

## Working style the user expects

- **Audit against the plan before declaring a milestone done.** They ask "is it complete?"
  and want the honest gap list, including scope that turned out obsolete.
- State trade-offs plainly; don't oversell. Flag deviations rather than letting them pass.
- Verify against live AWS, not just tests, before claiming a milestone is finished.
- Comments explain *why*, especially where a choice looks unusual.
