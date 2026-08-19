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

## Status

**Milestones 0–4 complete, deployed, and verified against live AWS.** M5 (hardening,
multi-tenancy, MCP proxy, load test) is next.

| | Scope | State |
|---|---|---|
| M0 | Foundation, CI, cost gate, health endpoints | deployed |
| M1 | Policy engine, `/v1/evaluate`, hash-chained audit | deployed |
| M2 | Enforcement SDK + a real Qwen3 agent | verified |
| M3 | Human-in-the-loop workflow + review console | deployed |
| M4 | Simulation harness, dry run, versioned policy | deployed |
| M5 | Hardening, multi-tenancy, MCP proxy, load test | next |

315 tests, `ruff` + `mypy --strict` clean, 16/16 policy conformance against the deployed
endpoint, and **$0.00** actual AWS spend.

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
uv run pytest                                    # 315 tests, no AWS needed
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

Python 3.12 · FastAPI · Mangum · AWS Lambda (arm64) · Lambda Function URL · DynamoDB
(provisioned) · AWS CDK · Ollama Qwen3 · uv · ruff · mypy strict · pytest · hypothesis

CloudFront was designed in and then **removed**: a new AWS account cannot create
distributions without a support case, so the Function URL is the edge and authentication
moved into the application, where it can be tested. That is the same posture as any public
API endpoint — the URL being reachable was never the control.
