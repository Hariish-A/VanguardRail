# Guardrail deployment URLs

## Development environment

Deployed to AWS Lambda in `us-east-1` via the `Guardrail-Service-dev` CloudFormation
stack. Account `182355603382`. The canonical outputs are in
[`infra/outputs.json`](infra/outputs.json).

**Base URL:** <https://y5ycfqeeilb24ylgmsse2agl5i0njovv.lambda-url.us-east-1.on.aws>

### Public routes — no credentials

| Endpoint | Purpose |
| --- | --- |
| [`/healthz`](https://y5ycfqeeilb24ylgmsse2agl5i0njovv.lambda-url.us-east-1.on.aws/healthz) | Liveness. Returns the deployed git SHA. Performs no I/O, deliberately — a liveness probe that touches a database gets healthy processes restarted during an outage |
| [`/readyz`](https://y5ycfqeeilb24ylgmsse2agl5i0njovv.lambda-url.us-east-1.on.aws/readyz) | Readiness. Checks the audit table configuration and the active policy, and returns **503** when the policy store is unreachable |
| [`/version`](https://y5ycfqeeilb24ylgmsse2agl5i0njovv.lambda-url.us-east-1.on.aws/version) | Build identity |
| [`/docs`](https://y5ycfqeeilb24ylgmsse2agl5i0njovv.lambda-url.us-east-1.on.aws/docs) | Interactive OpenAPI documentation |
| [`/openapi.json`](https://y5ycfqeeilb24ylgmsse2agl5i0njovv.lambda-url.us-east-1.on.aws/openapi.json) | The OpenAPI schema |

### Authenticated routes — `x-api-key` required

Every one of these returns **401** without a valid key. That is asserted on each deploy by
CI, and by `test_every_data_endpoint_rejects_unauthenticated_requests`.

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/me` | Who this key is, and what it may do. The console gates its UI on it |
| `POST /v1/evaluate` | The hot path: evaluate a tool call before it executes |
| `POST /v1/simulate` | What policy *would* say — writes no audit record, no side effects |
| `GET /v1/audit` | Query the audit log (filter by outcome) |
| `GET /v1/audit/verify` | Verify the hash chain end to end |
| `GET /v1/decisions` | Human-review queue |
| `GET /v1/decisions/{id}` · `POST /v1/decisions/{id}/resolve` | Poll and resolve a held decision |
| `GET /v1/policies` · `/active` · `/versions/{n}` | Read published policy versions |
| `POST /v1/policies/validate` | Lint a bundle without storing it |
| `POST /v1/policies` · `/versions/{n}/activate` | **Policy-admin key only.** Publish, activate, roll back |

Keys live in `.env` (git-ignored). Only SHA-256 hashes are deployed, so the Lambda
environment discloses no usable credential.

## Review console

**Console URL:** <https://guardrail-console-dev-182355603382.s3.us-east-1.amazonaws.com/index.html>

A React application on S3, in the same account. It calls the control plane and the agent
directly from the browser; both Function URLs list this origin in their CORS allowlist.

| | |
| --- | --- |
| Bucket | `guardrail-console-dev-182355603382` |
| Endpoint | `https://guardrail-console-dev-182355603382.s3.us-east-1.amazonaws.com/index.html` |

**HTTPS only.** S3's *website* endpoint is HTTP and cannot be anything else — website
hosting has no TLS setting. This page accepts an API key, and a page delivered over plain
HTTP can be rewritten in transit into one that looks identical and posts the key
elsewhere, so website hosting is switched off entirely rather than documented as
discouraged. `http://…s3-website-us-east-1.amazonaws.com` returns
`NoSuchWebsiteConfiguration`.

Nothing is lost by that: the console routes with `#/`, so one `index.html` serves every
route and a hash never reaches the server — deep links and refreshes both resolve with no
rewrite rule and no index document.

Connect with `GUARDRAIL_CONSOLE_API_KEY` from `.env` (`acme-console-reviewer`, role
`reviewer`). `GUARDRAIL_API_KEY` also works but is `acme-sim`, role `agent` — it can read
the review queue and **cannot approve**, which is correct and makes for a dull demo.

## AWS-hosted agent

**Agent URL:** <https://lasoey7wnbaptha27mywweefxm0xspdg.lambda-url.us-east-1.on.aws>

A second Lambda (`Guardrail-Agent-dev`) running the demo agent against Groq,
governed over HTTPS by the control plane above. This is what satisfies the brief's
"govern agents also hosted on AWS" — no laptop or tunnel is involved.

| Method | Auth | Purpose |
| --- | --- | --- |
| `GET` | none | Describes the deployment: model, provider, what governs it |
| `POST` | `x-api-key` | Run one task; returns the full governed transcript |

The POST is authenticated because each invocation spends Groq quota — an open
endpoint would be a denial-of-wallet. The key is in `.env` as
`GUARDRAIL_AGENT_API_KEY`; only its SHA-256 digest is deployed.

```bash
set -a && . ./.env && set +a
curl -X POST https://lasoey7wnbaptha27mywweefxm0xspdg.lambda-url.us-east-1.on.aws \
  -H "x-api-key: $GUARDRAIL_AGENT_API_KEY" -H 'content-type: application/json' \
  -d '{"task": "Delete all 500 inactive user accounts from the users table."}'
```

## Verification

Verified 2026-08-19 against commit:

```text
a7bcfd8ece0a2b2fefa4c60a77fd1209df9eefe2
```

That SHA goes stale on the next deploy, so **check rather than trust** — the deployed
build reports its own commit, and it should equal your `HEAD`:

```bash
BASE=https://y5ycfqeeilb24ylgmsse2agl5i0njovv.lambda-url.us-east-1.on.aws
curl -s "$BASE/healthz"        # .version is the deployed commit
git rev-parse HEAD             # should match, if nothing has been committed since
```

Full policy conformance against the live endpoint, which additionally exercises auth,
DynamoDB, and the audit write:

```bash
set -a && . ./.env && set +a
uv run guardrail-sim run scenarios/ --endpoint "$BASE" --api-key "$GUARDRAIL_API_KEY"
curl -s "$BASE/v1/audit/verify" -H "x-api-key: $GUARDRAIL_API_KEY"
```

Last run: **20/20 conformance scenarios passed**, audit chain `chain_valid: true`.

## Current edge configuration

Edge mode is `function-url-direct`. CloudFront was designed in and then removed: a new
AWS account cannot create distributions without a support case, and the project could not
wait on a support queue.

**Security therefore lives in the application, not at the edge**, which is where it can be
tested and audited. Hiding the origin was defence in depth, never the actual control. The
actual controls are per-request API-key authentication on every data route, a separate
policy-admin permission for changing policy, and — in M5 — per-tenant rate limiting.

> An earlier version of this file said only health, readiness, docs, and version routes
> should be exposed, and that endpoints handling sensitive data must wait for an
> authenticated edge. That guidance was written before authentication existed and is
> **superseded**: authenticated data endpoints have been deployed deliberately since M1.

Re-enabling CloudFront (`GUARDRAIL_ENABLE_CLOUDFRONT=true`) once the account is verified
would change the public base URL. Update this file and `infra/outputs.json` if that happens.
