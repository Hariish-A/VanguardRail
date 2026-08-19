# Guardrail load test

Run against the deployed `dev` control plane
(`https://y5ycfqeeilb24ylgmsse2agl5i0njovv.lambda-url.us-east-1.on.aws`) on 19 Aug 2026.

Reproduce with:

```bash
set -a && . ./.env && set +a
uv run python scripts/loadtest.py --endpoint "$BASE" --api-key "$GUARDRAIL_API_KEY" \
  --concurrency 2 --duration 120
```

**No LLM is involved.** The enforcement path never calls one, so including inference would
measure Ollama rather than the guardrail.

---

## Headline: what this deployment sustains

`--concurrency 2 --duration 120`

```
  requests        : 734  (6.1/s sustained)
  200 OK          : 734
  429 throttled   : 0
  5xx             : 0
  transport errors: 0

  Round-trip latency (client observed: network + Lambda + DynamoDB)
    p50 :    313.9 ms
    p95 :    337.7 ms
    p99 :    348.9 ms
    max :   4299.2 ms      <- one cold start

  Policy engine only (server-measured)
    p50 :      4.285 ms
    p95 :      5.722 ms
    p99 :     11.160 ms
    mean:      8.580 ms
```

**6.1 requests/second sustained for two minutes with zero errors and zero throttling.**

The two latency figures answer different questions. The engine number is the cost of
*governing* a tool call: **~4 ms at p50**, which is what an agent actually pays for
policy evaluation. The round-trip number adds the public internet, TLS, Lambda dispatch,
and the DynamoDB audit write — that ~300 ms is dominated by network distance to
`us-east-1`, not by the guardrail.

---

## The ceiling, and what happens at it

The audit table is provisioned at **5 WCU**, and every evaluation writes exactly one
hash-chained audit record. So roughly **5 writes/second is the sustainable ceiling**, and
6.1/s only clears it because DynamoDB lends burst capacity.

Pushed past it (`--concurrency 8 --duration 180`):

```
  requests        : 1917  (10.6/s attempted)
  200 OK          : 1093
  5xx             :  824   <- clean, fail-closed 503 + Retry-After
  Lambda errors   :    0
  max latency     : 7118 ms
```

Those 503s are **correct behaviour, not failure**. The service refuses to return a
decision it could not record, because permitting an unrecorded action is precisely the
gap this system exists to close. A fail-closed SDK blocks the tool call, and `Retry-After`
turns a retry storm into an orderly back-off.

A short test cannot find this ceiling. A 30-second run reported **18/s with zero errors**,
which is DynamoDB burst credit rather than capacity, and would have been a misleading
number to publish.

---

## A real bug this found

The first 180-second run produced **92 Lambda platform errors** with *nothing* in the
application log — no `audit_write_failed`, no ERROR line, no cause.

`DynamoDBAuditRepository.append` retried only `ConditionalCheckFailedException`. Anything
else re-raised on the first attempt — including
`ProvisionedThroughputExceededException`, which at 5 provisioned WCU is the most likely
failure the repository will ever see. It escaped as a raw `ClientError` and surfaced as an
unhandled **500**, not the fail-closed **503** that the SDK and the documentation both
promise, and it burned most of the 10-second function timeout on the way.

Fixed by treating throttling as a distinct retryable failure with harder, jittered
backoff; bounding the whole loop with a 5-second deadline that sits inside the Lambda
timeout; and capping botocore's own retries so they cannot compound with it.

After the fix, at the same concurrency: **Lambda platform errors 0**, max latency down
from 11.9 s to 7.1 s, `audit_write_failed` logged at ERROR with the cause named, and
nearly **3x more requests completed** in the same window.

This is the whole argument for load-testing a thing rather than reasoning about it. The
defect was invisible to review, to 375 unit tests, and to the conformance suite.

---

## Scaling beyond the free tier

Nothing here is architectural. The ceiling is one CDK constant:

| Change | Effect |
|---|---|
| `read_capacity` / `write_capacity` 5 -> 50 | ~10x sustained throughput; leaves the free tier |
| DynamoDB on-demand | Removes the ceiling entirely; **no free tier at all**, bills from request one |
| Lambda reserved concurrency | Currently impossible — the account quota is 10 total, and AWS requires 10 to stay unreserved |

The deployment is deliberately pinned to always-free limits. **Actual AWS spend to date:
$0.00.**
