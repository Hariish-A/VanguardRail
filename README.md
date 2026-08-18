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

**Milestone 0 complete** — foundation, CI/CD, cost gate, health endpoints. 36 tests
passing; infrastructure synthesizes clean. Not yet deployed (AWS account pending).

See [PROGRESS.md](PROGRESS.md) for status, decisions, and what you need to provide.
See [Project Documentation.md](Project%20Documentation.md) for concepts and architecture.

## Quick start

```bash
uv sync --all-extras
uv run pytest
uv run uvicorn guardrail_service.app:app --port 8080

curl http://localhost:8080/healthz
```

## Constraints

- **$0 AWS spend.** Always-free services only, enforced by a CI gate that fails the build
  if a paid resource appears in the synthesized template.
- **Free, open-weight LLM.** Ollama running Qwen3. No API key, no billing.
- **Every milestone ships deployed.**

## Stack

Python 3.12 · FastAPI · Mangum · AWS Lambda (arm64) · Lambda Function URL + CloudFront
(OAC) · DynamoDB · AWS CDK · Ollama Qwen3 · uv · ruff · mypy strict · pytest
