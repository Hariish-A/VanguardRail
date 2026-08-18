"""The client that sits between an agent and the guardrail.

This code runs in the hot path of every tool call an agent makes, so three properties
matter more than features:

**Fail closed by default.** If the guardrail cannot be reached, or cannot record its
decision, the action is refused. A governance control that disappears under load is not
a control. `fail_open=True` exists for teams who would rather keep agents running during
an outage, but it is an explicit, logged choice -- never a silent default.

**Retries are safe.** Every request carries an idempotency key, so a retry after a
dropped response returns the original decision instead of writing a second audit record
for one logical action. Without that, retrying would corrupt the record of what the agent
actually did.

**Failure is fast, not slow.** A circuit breaker stops hammering an unreachable service.
Under fail-closed that turns a multi-second timeout on every call into an immediate
refusal, which is the difference between an agent that is blocked and an agent that is
hung.
"""

from __future__ import annotations

import os
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from guardrail_sdk.exceptions import GuardrailUnavailable
from guardrail_sdk.models import Decision

DEFAULT_TIMEOUT = 5.0
"""Seconds. The engine itself answers in single-digit milliseconds, so anything near this
means the network or the service is unwell -- and waiting longer will not change that."""

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
"""Only transient failures are retried. A 4xx means the request itself is wrong, and
sending it again is pure latency."""


@dataclass
class CircuitBreaker:
    """Stops calling a service that is clearly down.

    Deliberately simple: a consecutive-failure count and a cooldown. A rolling error rate
    would be more nuanced, but this sits in the hot path and behaviour that is obvious
    under incident conditions beats behaviour that is optimal.
    """

    failure_threshold: int = 5
    cooldown_seconds: float = 10.0

    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    @property
    def is_open(self) -> bool:
        """True while the breaker is refusing calls."""
        with self._lock:
            if self._opened_at is None:
                return False
            if time.monotonic() - self._opened_at >= self.cooldown_seconds:
                # Half-open: let the next call through to test whether it recovered.
                self._opened_at = None
                self._failures = 0
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._opened_at = time.monotonic()


class GuardrailClient:
    """Evaluates tool calls against a deployed Guardrail control plane."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        fail_open: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 2,
        tenant_hint: str = "default",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Configuration falls back to the environment, so an agent can be governed
        without touching its code -- which is what makes adoption cheap.
        """
        resolved_url = base_url or os.environ.get("GUARDRAIL_BASE_URL", "")
        if not resolved_url:
            raise ValueError(
                "No guardrail URL. Pass base_url or set GUARDRAIL_BASE_URL to the "
                "deployed control plane."
            )

        self.base_url = resolved_url.rstrip("/")
        self.api_key = api_key or os.environ.get("GUARDRAIL_API_KEY", "")
        self.fail_open = fail_open
        self.max_retries = max_retries
        self.tenant_hint = tenant_hint
        self.breaker = CircuitBreaker()

        # One pooled client for the process. Establishing a TLS connection per tool call
        # would cost more than the policy decision itself.
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={"x-api-key": self.api_key, "content-type": "application/json"},
            transport=transport,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    # ------------------------------------------------------------------
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> GuardrailClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    def evaluate(
        self,
        *,
        tool: str,
        arguments: dict[str, Any],
        agent_id: str,
        session_id: str,
        principal: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        dry_run: bool = False,
        idempotency_key: str | None = None,
    ) -> Decision:
        """Ask whether a tool call may proceed.

        Raises GuardrailUnavailable when the service cannot answer and the client is
        configured to fail closed.
        """
        payload: dict[str, Any] = {
            "agent_id": agent_id,
            "session_id": session_id,
            "tool": tool,
            "arguments": arguments,
            "dry_run": dry_run,
            # Generated per logical action, not per HTTP attempt, so a retry is
            # recognised as the same action rather than a second one.
            "idempotency_key": idempotency_key or str(uuid.uuid4()),
        }
        if principal:
            payload["principal"] = principal
        if context:
            payload["context"] = context

        if self.breaker.is_open:
            return self._unavailable(tool, "circuit breaker open after repeated failures")

        try:
            response = self._post_with_retries("/v1/evaluate", payload)
        except httpx.HTTPError as exc:
            self.breaker.record_failure()
            return self._unavailable(tool, f"{type(exc).__name__}: {exc}")

        if response.status_code >= 500:
            self.breaker.record_failure()
            return self._unavailable(tool, f"service returned {response.status_code}")

        if response.status_code == 401:
            # Not transient and not retryable: an invalid key stays invalid. Raised even
            # in fail-open mode, because running unauthenticated is a configuration bug
            # the operator must see, not an outage to ride out.
            raise GuardrailUnavailable("authentication rejected (check GUARDRAIL_API_KEY)", tool)

        if response.status_code >= 400:
            self.breaker.record_failure()
            return self._unavailable(tool, f"request rejected: {response.text[:200]}")

        self.breaker.record_success()
        return Decision.model_validate(response.json())

    # ------------------------------------------------------------------
    def _post_with_retries(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        """POST, retrying transient failures with exponential backoff and full jitter.

        Jitter matters more than it looks: when a guardrail recovers, every waiting agent
        retries at once, and synchronised retries knock it straight back over.
        """
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = self._http.post(path, json=payload)
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if response.status_code not in RETRYABLE_STATUS:
                    return response
                last_error = httpx.HTTPError(f"status {response.status_code}")

            if attempt < self.max_retries:
                backoff = min(0.25 * (2**attempt), 2.0)
                time.sleep(random.uniform(0, backoff))  # noqa: S311 - jitter, not crypto

        raise last_error or httpx.HTTPError("request failed")

    def _unavailable(self, tool: str, reason: str) -> Decision:
        """Decide what an unanswerable evaluation means.

        Fail-closed raises. Fail-open synthesises an allow and marks it, so the audit
        story is honest: the decision came from the client's degraded mode, not from
        policy, and it is distinguishable from a real allow forever after.
        """
        if not self.fail_open:
            raise GuardrailUnavailable(reason, tool)

        return Decision(
            decision="allow",
            allowed=True,
            decision_id=f"fail-open-{uuid.uuid4()}",
            message=(
                f"Guardrail unavailable ({reason}); permitted because this client is "
                "configured to fail open. This decision was NOT evaluated against policy."
            ),
        )

    # ------------------------------------------------------------------
    def health(self) -> bool:
        """Whether the control plane is reachable. For startup checks, not the hot path."""
        try:
            return self._http.get("/healthz").status_code == 200
        except httpx.HTTPError:
            return False
