# Architecture decision records

Each entry states the decision, why it was taken, and — where it matters — what it costs.
Several of these overrode the original plan, and those say so.

---

## ADR-001 — Lambda Function URL instead of API Gateway

**Decision.** Serve the control plane from a Lambda Function URL.

**Why.** API Gateway's 1M requests/month is a 12-month introductory offer; after that HTTP
APIs cost $1.00/million. A Function URL carries no per-request charge, ever.

**Cost.** Everything API Gateway would have managed moves into the app: authentication,
rate limiting, request validation, throttling. Roughly 200 lines — and arguably better
owned in code where it is testable.

---

## ADR-002 — DynamoDB provisioned, never on-demand

**Decision.** Provisioned 5 WCU / 5 RCU per table and index, autoscaling off.

**Why.** The free tier covers 25 WCU + 25 RCU of *provisioned* capacity. **On-demand has
no free tier and bills from the first request.** This was an error in the original plan,
caught before deployment.

**Cost.** A hard throughput ceiling around 5 writes/second. Autoscaling stays off so a
spike throttles rather than silently generating a bill. Measured in
[`reports/loadtest.md`](../reports/loadtest.md).

---

## ADR-003 — Most-restrictive resolution, not first-match

**Decision.** Evaluate every enabled rule; the strictest matching effect wins.

**Why.** Under first-match-wins, reordering a policy file changes behaviour. Moving a broad
`allow` above a narrow `block` disables the block with no error and a diff that looks like
a tidy-up. Order-independence removes an entire class of silent security regression.

**Cost.** Every rule is evaluated on every action. At single-digit rule counts this is
microseconds; the engine measures p50 4.3 ms end to end.

---

## ADR-004 — UNKNOWN resolves toward restriction

**Decision.** An extractor that cannot determine a value returns `UNKNOWN`, and a predicate
over `UNKNOWN` is treated as matching for restrictive rules and not matching for permissive
ones.

**Why.** `DELETE ... WHERE last_login < '2023-01-01'` has a row count the engine cannot
know. Guessing "probably small" is how a guardrail silently stops guarding.

**Cost.** False positives: an unmeasurable action is refused even when it was harmless.
`unknown_paths` is returned on every decision so this is diagnosable rather than mysterious.

---

## ADR-005 — Hash-chained audit, stored as canonical JSON strings

**Decision.** `hash = sha256(prev_hash || canonical_json(payload))`, and the payload is
stored as the exact JSON string that was hashed.

**Why.** DynamoDB cannot store Python floats, and tool arguments legitimately contain them
(a refund of `1000.50`). Converting to Decimal and back risks a payload that does not
round-trip byte-identically, which would recompute a different hash and raise a **false
tamper alarm** — worse than a missed one, because it destroys trust in the log and fires
during an audit.

**Cost.** The payload is not queryable inside DynamoDB. Indexed scalars are lifted out
alongside it.

---

## ADR-006 — Read-latest plus conditional put, not transactions

**Decision.** Sequence the chain by reading the head and writing with
`attribute_not_exists(sk)`, retrying on contention.

**Why.** A transaction would be simpler, but DynamoDB transactions consume **double** the
write capacity, and the entire free allowance is 25 WCU.

**Cost.** Two operations per append instead of one, and a retry loop. See ADR-013.

---

## ADR-007 — The IAM role has no `DeleteItem`

**Decision.** Grant `PutItem`, `GetItem`, `Query`, `UpdateItem`. Never `DeleteItem`.

**Why.** A governance system whose own role can erase its evidence offers materially weaker
assurance than one that cannot. It also makes the policy version history append-only *by
permission* rather than by convention.

**Cost.** Expired decisions rely on DynamoDB TTL, performed by the service principal rather
than by this role.

---

## ADR-008 — CloudFront removed; security moved into the application

**Decision.** The Function URL is the public edge. CloudFront is optional and off.

**Why.** A brand-new AWS account cannot create distributions — it needs a support case and
24–48 hours. Making the architecture depend on a support queue was unacceptable.

**Cost.** No edge filtering and no origin hiding. Hiding the origin was defence in depth,
never the actual control; the actual control is per-request authentication. This is the
same posture as any public API endpoint.

---

## ADR-009 — HITL and policy share the audit table

**Decision.** Pending decisions and versioned policy bundles live in the audit table under
their own key prefixes.

**Why.** A separate table needs its own provisioned capacity and 15 of the free 25 WCU were
already committed. Pending decisions reuse the outcome index as a **sparse index** — the
index attributes exist only while pending — so resolved decisions leave the queue with no
scan, no filter, and no extra capacity.

**Cost.** Key design is doing more work. Mitigated by a test asserting policy items land in
a different partition from the audit chain.

---

## ADR-010 — Policy hot reload is a timed re-check on the request path

**Decision.** Cache the active bundle per warm container; re-check the pointer every 30
seconds.

**Why.** Lambda freezes containers between invocations, so a background poller or a
subscription would never run. The only place to re-check is the request path.

**Cost.** One small eventually-consistent read per container per 30 seconds, and a bounded
staleness window. A store outage keeps serving the last known good bundle; a cold start
during an outage falls back to the packaged bundle, logs at ERROR, and is surfaced by
`/readyz`. That last case can be a temporary *loosening*, which is why it is never silent.

---

## ADR-011 — Policy administration is a separate permission, closed by default

**Decision.** `GUARDRAIL_POLICY_ADMIN_KEY_IDS` lists who may publish or activate. It
defaults to empty, meaning nobody.

**Why.** An agent whose key can rewrite the policy governing it is not governed — it can
approve its own next action. The alternative default, "any authenticated caller", is
insecure the moment one agent key leaks, and insecure *silently*.

**Cost.** Publishing fails with a 403 until an operator configures it. Deliberate friction.

---

## ADR-012 — In-process rate limiting, not a DynamoDB token bucket

**Decision.** Per-tenant token buckets held in each warm container.

**Why.** The plan specified a DynamoDB bucket. At 5 provisioned WCU that costs one write
per request, caps the service near 5 req/s, and spends the very capacity the audit chain
needs — protecting a service from the constraint it is already bound by.

**Cost.** The real global bound is `containers x per-container rate`, not the per-container
rate. This is a genuine weakening and it is reported by `global_ceiling()` rather than
hidden. **It is not a fair-share mechanism.** A shared counter becomes correct once the
capacity ceiling is lifted, and drops in behind the same interface.

---

## ADR-013 — Throttling is a first-class retryable failure

**Decision.** `append` retries throttling with harder jittered backoff, bounded by a
5-second deadline inside the 10-second Lambda timeout, with botocore's own retries capped.

**Why.** Originally only `ConditionalCheckFailedException` was retried; everything else
re-raised on the first attempt. At 5 provisioned WCU,
`ProvisionedThroughputExceededException` is the most likely failure the repository will
ever see, and it escaped as a raw `ClientError` — surfacing as an unhandled **500** rather
than the fail-closed **503** the SDK and docs both promise, with no log line naming the
cause. A 180-second load test produced 92 of them; review, 375 unit tests, and the
conformance suite had all missed it.

**Cost.** Up to 5 seconds spent before failing. Worth it: the alternative was being killed
by the function timeout, which logs nothing at all.

---

## ADR-014 — Simulation is never audited

**Decision.** `/v1/simulate` writes no audit record and creates no pending decision.

**Why.** `/v1/evaluate` records what an agent *attempted*. A simulation is not an attempt.
Recording thousands of what-ifs beside real decisions would dilute the one log meant to
answer "what did this agent do", forcing every query to separate evidence from speculation.

**Cost.** Policy exploration leaves no trace in the chain. It is still emitted as a
structured log line, so the activity remains visible in CloudWatch.

---

## ADR-015 — Rollback is activation of an earlier version

**Decision.** No separate rollback endpoint.

**Why.** A distinct rollback path is code that runs only during incidents — the worst
possible test-coverage profile for the operation you most need to work. Reusing activation
means the rollback path is exercised by every ordinary activation, and a rollback can only
ever land on a bundle that was reviewed and stored.

**Cost.** None identified.

---

## ADR-016 — Zip and layer, not a container-image Lambda

**Decision.** Deploy a zip plus a dependency layer. Keep a Dockerfile for local parity, CI,
and portability.

**Why.** Container-image Lambdas require a *private* ECR repository: 500 MB free for 12
months, then $0.10/GB-month. The dependency set is 60–80 MB, well inside the 250 MB zip
limit, so the 10 GB image ceiling buys nothing and would eventually cost money. A smaller
artifact also cold-starts faster, and this sits in front of every tool call.

**Cost.** Two build paths. The image is now verified by
[`scripts/portability_proof.py`](../../scripts/portability_proof.py) — which was added
after discovering the image had never actually been built and did not start.

---

## ADR-017 — Reserved concurrency left unset

**Decision.** Do not set per-function reserved concurrency.

**Why.** Not a preference — it is impossible here. A new AWS account has a total
`ConcurrentExecutions` quota of **10**, and AWS rejects any reservation that would leave
fewer than 10 unreserved. The maximum reservable value is exactly zero, and the first
deploy attempt failed on precisely that.

**Cost.** No per-function isolation. The account-wide quota provides the same ceiling while
there is one function. The setting is honoured when present, so raising the quota is a
support request rather than a code change.

---

## ADR-018 — No S3 archive, no X-Ray, no Cognito (yet)

**Decision.** Three planned components deliberately not built.

**Why.**

* **S3 audit archive** — S3's free tier is 12 months only. DynamoDB gives 25 GB of
  always-free storage and bundles are kilobytes, so archiving would add a
  pays-after-a-year dependency for no benefit at this volume.
* **X-Ray** — not in the always-free tier. Structured logs plus Logs Insights answer the
  same questions against the far larger 5 GB log allowance.
* **Cognito console sign-in** — genuinely free (10k MAU, never expires), but substantial
  work, and the console currently authenticates with an API key in `sessionStorage`.
  Acceptable for a single-reviewer demo; **not** acceptable for production, and recorded
  as a gap in the threat model.

**Cost.** Named in [`threat-model.md`](threat-model.md) rather than left implicit.
