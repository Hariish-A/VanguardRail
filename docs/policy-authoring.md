# Writing policy

A practical guide to the bundle format, aimed at whoever has to change a rule during an
incident.

---

## The shape of a bundle

```yaml
apiVersion: guardrail/v1

metadata:
  bundle_id: default
  version: 1              # the STORE assigns this on publish; yours is ignored
  description: Baseline action policy for governed agents.
  mode: enforce           # enforce | shadow

defaults:
  effect: allow           # what happens when nothing matches
  resolution: most_restrictive

rules:
  - id: db-bulk-delete
    description: Block destructive deletes whose blast radius exceeds the threshold.
    severity: critical
    match:
      tool: db.delete_records
      all:
        - path: derived.record_count
          op: gt
          value: 100
    effect: block
    message: "Blocked: this would delete {derived.record_count} records."
```

---

## The four effects

| Effect | The action | Recorded? |
|---|---|---|
| `allow` | Runs | Yes — an audit log that only records denials cannot answer "what did this agent do last Tuesday" |
| `log_and_allow` | Runs | Yes, flagged as noteworthy |
| `require_hitl` | Suspended until a human answers | Yes, plus a pending decision |
| `block` | Never dispatched | Yes |

## Rule order does not matter

**Every enabled rule is evaluated and the strictest matching effect wins.** This is the
single most important thing to know about the format.

Under first-match-wins, moving a broad `allow` above a narrow `block` silently disables the
block — no error, and a diff that looks like a tidy-up. Here, reordering the file cannot
change any outcome. Every matched rule is recorded, not just the winner, so a reviewer can
see that an action tripped four rules even though one dominated.

---

## Matching

```yaml
match:
  tool: db.*        # exact name or glob; omit to match every tool
  all: [...]        # AND
  any: [...]        # OR
```

`all` and `any` may both appear; both must then be satisfied. A `match` block with no
`tool`, `all`, or `any` is **rejected at load time** — it would apply to every action, and
that is far more likely to be a mistake than an intention.

### Paths a predicate can read

| Root | Contains |
|---|---|
| `args.*` | Exactly what the agent submitted |
| `derived.*` | Normalised facts — **prefer these** |
| `context.*` | Ambient facts the caller supplied, e.g. `environment: production` |
| `principal.*` | Who is acting, and on whose behalf |
| `tool`, `tenant_id`, `agent_id` | Scalars |

### Operators

`eq` `ne` `gt` `gte` `lt` `lte` `in` `not_in` `any_in` `any_not_in` `contains` `icontains`
`matches` `glob` `exists`

The set is **closed**. An unknown operator is a load-time error, never a silently skipped
predicate — a typo must not become a disabled security rule.

---

## Derived facts, and why to prefer them

The same intent arrives in many shapes. Extractors normalise it so one rule covers all of
them:

```
args.count = 500                 ─┐
args.ids = [1, 2, ... 500]        ├─►  derived.record_count = 500
args.where = "last_login < ..."  ─┘    (or UNKNOWN)
```

| Fact | Meaning |
|---|---|
| `derived.record_count` | Blast radius of a mutation, as a row count |
| `derived.recipient_domains` | Lowercased domains across `to`, `cc`, **and `bcc`** |
| `derived.recipient_count` | How many addresses in total |
| `derived.path` | Resource path, normalised to forward slashes |

A rule written against `args.to` would miss a `bcc`. A rule written against `args.path`
would miss `C:\Users\...\.ssh\id_rsa`. **Write rules against `derived.*` unless you
specifically mean the raw argument.**

## UNKNOWN, and why a rule can fire on nothing

When an extractor cannot determine a value it returns `UNKNOWN` rather than guessing.
`UNKNOWN` then resolves **toward whichever answer restricts**:

* on a `block` or `require_hitl` rule, the predicate is treated as **matching**
* on an `allow` or `log_and_allow` rule, as **not matching**

So `DELETE ... WHERE <anything>` trips the bulk-delete rule, because the row count is
unknowable from the request and an unmeasured deletion is the dangerous case. If you see a
block you did not expect, check `unknown_paths` in the response first — it is usually this.

---

## Messages

```yaml
message: "Blocked: this would delete {derived.record_count} records, above the limit of 100."
```

Placeholders are fact paths. `UNKNOWN` renders as `unknown`; an unresolvable placeholder is
left as written so the defect is visible rather than crashing an evaluation.

**Write the message for the model, not for a log.** The agent reads this and relays it. "That
is not allowed" invites a retry loop; naming the rule and the number tells the agent what to
do instead.

---

## Human review

```yaml
effect: require_hitl
hitl:
  timeout_seconds: 900
  on_timeout: deny        # deny | allow
  reviewers: [security-oncall]
```

`on_timeout` defaults to `deny`, and should almost always stay there: **silence is not
consent.** An approval request nobody answers must not become an approval.

Expiry is computed at read time, not delegated to DynamoDB TTL — TTL deletes on a
best-effort schedule that can lag by 48 hours, so trusting it would leave a request that
expired an hour ago still answerable.

---

## Shadow mode

```yaml
metadata:
  mode: shadow
```

Downgrades every `block` and `require_hitl` to `log_and_allow` for the whole bundle, so a
policy can be trialled against live traffic before it restrains anything. Matched rules are
still recorded — otherwise shadow mode would teach you nothing.

---

## The workflow

```bash
# 1. Does it parse, and do the rules exist?
uv run guardrail-sim validate scenarios/ --policy policies/default.yaml

# 2. Does it still do what the scenarios say?
uv run guardrail-sim run scenarios/ --policy policies/default.yaml

# 3. What would this change do to real traffic?
uv run guardrail-sim diff scenarios/ --endpoint "$BASE" --api-key "$KEY" \
  --candidate policies/candidate.yaml

# 4. Store it (this changes nothing)
curl -X POST "$BASE/v1/policies" -H "x-api-key: $ADMIN_KEY" \
  -H 'content-type: application/json' -d '{"bundle": {...}}'

# 5. Make it live (this changes everything)
curl -X POST "$BASE/v1/policies/versions/3/activate" -H "x-api-key: $ADMIN_KEY"

# 6. Roll back — the same call, a lower number
curl -X POST "$BASE/v1/policies/versions/2/activate" -H "x-api-key: $ADMIN_KEY"
```

Warm containers pick up an activation within `GUARDRAIL_POLICY_REFRESH_SECONDS` (30).
Read the `diff` output for **LOOSER** first: those are actions that would stop being
governed.

> **Both copies of the policy must match.** `policies/default.yaml` is what CI validates;
> `packages/guardrail-service/.../policies/default.yaml` is what the Lambda bundles. A test
> asserts they are byte-identical, because editing one and not the other would leave the
> conformance suite green while production behaved differently.

---

## Pitfalls

**A scenario that asserts only the outcome.** It keeps passing after the rule it was
written for is deleted, as long as something else blocks the same action. Always assert
`rules:`.

**A typo in `rules_absent`.** An id that does not exist can never match, so the assertion
passes forever. `guardrail-sim validate` is the only thing that catches it.

**Two rules covering one action.** Deleting one changes nothing today and everything
tomorrow. `guardrail-sim diff` reports "same outcome, different rules" for exactly this.

**Assuming `version:` in your file matters.** The store assigns it. Two people both typing
`version: 2` would otherwise overwrite each other, and `bundle_version` in the audit log
would stop identifying a specific policy.
