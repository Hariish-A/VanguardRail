# Guardrail

**Action-layer governance for AI agents.** Policy enforcement at the moment a tool call
is about to execute — not after the text is generated.

Every commercial guardrails platform filters LLM text. None govern what an agent *does*
afterwards. A perfectly clean model response can still instruct a tool to delete ten
thousand database records. Guardrail evaluates the **action** against a declarative policy
bundle **before dispatch**, returning `allow`, `log_and_allow`, `require_hitl`, or `block`
— with a tamper-evident audit record for every decision.

```
LLM output ──► parse tool_calls ──► [ GUARDRAIL ] ──► execute (or not)
```

**Live console:** <https://guardrail-console-dev-182355603382.s3.us-east-1.amazonaws.com/index.html>

Open it, paste an API key, and drive the whole system from a browser — run the
AWS-hosted agent, watch an action be held before it executes, approve it, and verify the
audit chain. Nothing runs locally.

## Status

**All seven milestones complete, deployed, and verified against live AWS.**

| | Scope | State |
|---|---|---|
| M0 | Foundation, CI, cost gate, health endpoints | deployed |
| M1 | Policy engine, `/v1/evaluate`, hash-chained audit | deployed |
| M2 | Enforcement SDK + a real Qwen3 agent | verified |
| M3 | Human-in-the-loop workflow + review console | deployed |
| M4 | Simulation harness, dry run, versioned policy | deployed |
| M5 | Hardening, multi-tenancy, MCP proxy, load test | deployed |
| M6 | React console on AWS, roles surfaced in the UI, `/v1/me` | deployed |
| M7 | Policy Studio, change-impact diff, playground, dry-run, conformance, MCP view | deployed |

441 Python tests plus 70 console tests, `ruff` + `mypy --strict` clean, **20/20**
policy conformance against the deployed endpoint *and* against a plain container, and
**$0.00** actual AWS spend.

Measured, not estimated: **6.1 req/s sustained** with zero errors, and a policy engine
at **p50 4.3 ms / p99 11 ms** — see the [load test report](reports/loadtest.md).

Prove it in one command, with no AWS account and no credentials:

```bash
uv run guardrail-sim run scenarios/ -v
```

`PROGRESS.md` and `Project Documentation.md` are kept out of git at the author's request
but are on disk: **PROGRESS.md** is the running record (status, decisions, bugs, and a
resume-from-cold runbook); **Project Documentation.md** explains the concepts, the policy
model, and the architecture. `CLAUDE.md` holds the short form of both.

## Quick start

```bash
uv sync --all-extras
uv run pytest                                    # 441 tests, no AWS needed
uv run guardrail-sim run scenarios/ -v           # policy conformance, offline
uv run uvicorn guardrail_service.app:app --port 8080

curl http://localhost:8080/healthz
```

Against the deployed control plane (an API key is required — data endpoints reject
unauthenticated callers by design):

```bash
BASE=https://y5ycfqeeilb24ylgmsse2agl5i0njovv.lambda-url.us-east-1.on.aws
curl -s $BASE/healthz
uv run guardrail-sim run scenarios/ --endpoint $BASE --api-key "$GUARDRAIL_API_KEY"   --junit reports/conformance.xml --html reports/conformance.html
```

## An agent on AWS, governed by the control plane on AWS

The demo agent also runs as its own Lambda, so the brief's *"govern agents also
hosted on AWS"* is satisfied literally. It uses Groq for inference, so it works
whenever anyone runs it — no laptop, no tunnel:

```
POST /  ──►  agent Lambda  ──HTTPS──►  guardrail Lambda  ──►  audit chain
                  │
                  └──HTTPS──►  Groq
```

```bash
curl -X POST https://lasoey7wnbaptha27mywweefxm0xspdg.lambda-url.us-east-1.on.aws \
  -H "x-api-key: $GUARDRAIL_AGENT_API_KEY" -H 'content-type: application/json' \
  -d '{"task": "Delete all 500 inactive user accounts from the users table."}'
```

It returns the full transcript: every tool the model tried, what policy decided,
which rule fired, **and the audit sequence number** — so the same decision can be
found afterwards in `/v1/audit`. Plus the side-effect ledger, which is what makes
"blocked" verifiable rather than merely claimed.

All four outcomes verified live from AWS: bulk delete **blocked** (nothing executed),
external email **held for review** then approved by a human, internal email
**allowed**, confidential read **logged and allowed**.

## The console

`apps/console-ui` — React 19 + Vite + TypeScript, served from S3, talking to the control
plane over HTTPS. Six screens:

Twelve screens in three groups — *Operate*, *Policy*, *Evidence* — because those are
three different jobs usually done by three different people.

| Page | Answers |
|---|---|
| Overview | What an action guardrail is, and what has actually been proven |
| Agent Console | Runs the AWS-hosted agent and shows the governed transcript |
| Decision Theatre | Send any tool call; see the verdict, the rules, the derived facts |
| Review Queue | Approve or deny an action held before execution |
| Audit & Chain | Every decision, and proof none were edited |
| Policy Studio | Author, validate, publish, activate, roll back — publishing and activating deliberately separate |
| Change Impact | Which decisions a candidate policy would change, and which would become **permitted** |
| Playground | Probe any version; find rules that fire for *nothing* |
| Dry-run & Shadow | Three levels of evaluate-without-enforcing, and a parity run proving they agree |
| Conformance | The real `scenarios/*.yaml` corpus, run against live AWS from the browser |
| MCP Proxy | Governing a tool server that knows nothing about Guardrail |
| System Health | Liveness, readiness, and the free-tier ceilings that shape the design |

**No credential is baked into the bundle** — a deployed frontend is a world-readable
artifact, and CI fails the build if a key-shaped string appears in it. The reviewer pastes
their own key; it is verified against `/v1/me` before it is stored, and held only in
`sessionStorage`.

What the console renders is decided by the **server**. `/v1/me` returns the caller's
capabilities, and a test asks the server what a key may do and then goes and tries every
verb, failing if the two disagree in either direction. A key with the `agent` role can see
the review queue and cannot act on it — because an agent that can approve the action its
own policy held has not been governed.

```bash
cd apps/console-ui && npm ci && npm run dev    # http://localhost:5173
```

## Governing a tool server that knows nothing about Guardrail

`guardrail-mcp` proxies any MCP server and enforces policy on every `tools/call`, with
**no changes to the server**:

```bash
uv run python scripts/mcp_demo.py
```

That runs the real `@modelcontextprotocol/server-filesystem`, asks it for a private key,
and checks the request never reached it — a canary string inside the key file must be
absent from the entire transcript. An ordinary read still succeeds, and a write is held
for human review with the file untouched on disk.

## What it does

| Endpoint | Purpose |
|---|---|
| `POST /v1/evaluate` | The hot path. Action in, decision out, audit record written |
| `POST /v1/simulate` | What *would* policy say — no audit record, no side effects |
| `GET /v1/audit` · `/v1/audit/verify` | Query the log; verify the hash chain end to end |
| `GET /v1/decisions` · `POST /v1/decisions/{id}/resolve` | Human review queue |
| `GET`/`POST` `/v1/policies` · `/versions/{n}/activate` | Publish, activate, roll back |

## Constraints

- **$0 AWS spend.** Always-free services only, enforced by a CI gate that fails the build
  if a paid resource appears in the synthesized template.
- **Free, open-weight LLM.** Ollama running Qwen3. No API key, no billing.
- **Every milestone ships deployed.**

## Stack

**Control plane** — Python 3.12 · FastAPI · Mangum · AWS Lambda (arm64) · Lambda
Function URL · DynamoDB (provisioned) · AWS CDK · Ollama Qwen3 / Groq · uv · ruff · mypy
strict · pytest · hypothesis

**Console** — React 19 · Vite 6 · TypeScript strict · Tailwind v4 · framer-motion ·
vitest + Testing Library · S3 static hosting

Also included: an MCP guardrail proxy, LangChain and OpenAI-compatible adapters, a
scenario conformance harness, and a threat model — see [docs/](docs/).

CloudFront was designed in and then **removed**: a new AWS account cannot create
distributions without a support case, so the Function URL is the edge and authentication
moved into the application, where it can be tested. That is the same posture as any public
API endpoint — the URL being reachable was never the control.
