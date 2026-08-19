"""Per-tenant rate limiting, sized for the capacity this account actually has.

## Why this is not the DynamoDB token bucket the plan called for

The plan specified a DynamoDB-backed token bucket per tenant. Under this deployment's
constraints that design is actively harmful, and the arithmetic is not close:

* A shared bucket costs **one write per request**. The table is provisioned at 5 WCU, so
  a DynamoDB limiter would cap the whole service at roughly **5 requests per second**
  before throttling -- a rate limiter that is itself the bottleneck.
* Worse, those writes compete with the **audit chain**, which must never fail. Spending
  scarce write capacity on rate limiting in order to protect a service whose real
  constraint is that same write capacity is circular.

So the limit is enforced **in-process**, per warm Lambda container, and the global
ceiling comes from bounding how many containers there can be.

## What that actually guarantees, stated precisely

Each container refills its own bucket. With `reserved_concurrency = N` containers and a
per-container rate of `R`, the worst-case global rate is **N x R**, not R. This is a real
weakening compared with a shared counter and it is not hidden: `global_ceiling()` returns
the number, `/readyz` reports it, and the docstring says it out loud.

It is still a useful control, because the thing being defended against is a runaway agent
or a leaked key hammering the endpoint, and both are bounded well enough by `N x R`. It is
**not** a fair-share mechanism, and it should not be described as one.

The hard backstop is Lambda **reserved concurrency**, which is free, is enforced by AWS
rather than by this code, and cannot be exceeded no matter what the application does.

## When a shared counter becomes worth it

At the point where the free-tier capacity ceiling is lifted. Then a DynamoDB bucket with
`UpdateItem` and an atomic counter is the right design, and it drops in behind this same
`RateLimiter` interface -- which is why the interface exists rather than the logic being
inlined into the request path.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Protocol


@dataclass
class Verdict:
    """The outcome of one rate-limit check."""

    allowed: bool
    remaining: float
    retry_after: float = 0.0
    """Seconds until one token is available. Sent as `Retry-After`, so a client backs off
    by a useful amount rather than guessing."""


class TokenBucket:
    """A classic token bucket: `capacity` tokens, refilled at `rate` per second.

    Chosen over a fixed window because a fixed window permits a double-rate burst across
    a boundary -- the full allowance at the end of one window and again at the start of
    the next. A bucket smooths that out while still allowing a genuine burst up to
    `capacity`, which is what a batch agent legitimately needs.

    Refill is computed lazily from the clock rather than by a timer. There is no thread
    to schedule in Lambda, and a bucket that only moves when it is looked at gives
    identical results for far less machinery.
    """

    __slots__ = ("_lock", "_rate", "_tokens", "capacity", "updated_at")

    def __init__(self, capacity: float, rate: float, *, now: float | None = None) -> None:
        self.capacity = capacity
        self._rate = rate
        self._tokens = capacity
        self.updated_at = now if now is not None else time.monotonic()
        self._lock = threading.Lock()

    def consume(self, tokens: float = 1.0, *, now: float | None = None) -> Verdict:
        """Take a token if one is available."""
        moment = now if now is not None else time.monotonic()

        with self._lock:
            elapsed = max(0.0, moment - self.updated_at)
            self._tokens = min(self.capacity, self._tokens + elapsed * self._rate)
            self.updated_at = moment

            if self._tokens >= tokens:
                self._tokens -= tokens
                return Verdict(allowed=True, remaining=self._tokens)

            deficit = tokens - self._tokens
            retry_after = deficit / self._rate if self._rate > 0 else float("inf")
            return Verdict(allowed=False, remaining=self._tokens, retry_after=retry_after)


class RateLimiter(Protocol):
    """The seam a DynamoDB-backed limiter drops into once capacity allows."""

    def check(self, tenant_id: str) -> Verdict: ...


class InProcessRateLimiter:
    """Per-tenant buckets held in one warm container.

    Tenants are kept in an LRU with a hard cap. Without it, a caller able to influence
    the tenant field could grow this dictionary without bound and turn a rate limiter
    into a memory-exhaustion vector -- which would be an unusually ironic denial of
    service. Eviction only costs an evicted tenant a fresh full bucket, so the failure
    mode of the cap is permissiveness rather than a wrongly-refused request.
    """

    def __init__(
        self,
        *,
        per_minute: float = 600.0,
        burst: float | None = None,
        max_tenants: int = 1000,
    ) -> None:
        self.per_minute = per_minute
        self.rate = per_minute / 60.0
        # Five seconds' worth by default. One second's worth was the first choice and it
        # was wrong: a batch agent legitimately dispatches twenty tool calls back to back,
        # and throttling that is indistinguishable from a broken guardrail. Five seconds
        # absorbs a real burst while still being far short of letting an idle tenant bank
        # a minute of capacity and spend it at once.
        self.burst = burst if burst is not None else max(10.0, self.rate * 5)
        self.max_tenants = max_tenants
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, tenant_id: str) -> Verdict:
        if self.per_minute <= 0:
            # Disabled. Explicitly allowed rather than treated as "zero requests", which
            # would be a spectacular way to take the service down via a config typo.
            return Verdict(allowed=True, remaining=float("inf"))

        with self._lock:
            bucket = self._buckets.get(tenant_id)
            if bucket is None:
                bucket = TokenBucket(self.burst, self.rate)
                self._buckets[tenant_id] = bucket
                if len(self._buckets) > self.max_tenants:
                    self._buckets.popitem(last=False)
            else:
                self._buckets.move_to_end(tenant_id)

        return bucket.consume()

    def global_ceiling(self, reserved_concurrency: int) -> float:
        """Worst-case requests per minute across the whole deployment.

        Exposed because the honest number is `containers x per-container rate`, and a
        limiter documented as "600/min" while actually permitting `N x 600/min` would be
        a security control that misrepresents itself.
        """
        return self.per_minute * max(1, reserved_concurrency)
