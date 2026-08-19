"""Structured logging, metrics, and tracing.

Three deliberate choices, all driven by the zero-cost constraint in the plan:

1. Logs are JSON so CloudWatch Logs Insights can answer per-rule and per-tenant
   questions. That keeps fine-grained analysis on the 5 GB log allowance rather
   than the far scarcer 10-custom-metric allowance.
2. Metrics use Embedded Metric Format, which CloudWatch bills as custom metrics —
   and each unique name + dimension combination counts separately. Hence
   METRIC_BUDGET below.
3. Tracing is constructed lazily. Powertools' Tracer imports `aws_xray_sdk` at
   construction time, so building one unconditionally would drag the SDK into the
   Lambda zip for a service that does not enable X-Ray until M5 — and even then
   only at low sampling, because X-Ray is not always-free.

Log retention (7 days) is set in the CDK stack rather than here, so the allowance
is enforced by infrastructure instead of convention.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aws_lambda_powertools import Logger, Metrics

from guardrail_service.config import get_settings

if TYPE_CHECKING:
    from aws_lambda_powertools import Tracer

_settings = get_settings()

logger: Logger = Logger(
    service=_settings.service_name,
    level=_settings.log_level,
    use_rfc3339=True,
)

metrics: Metrics = Metrics(
    namespace=_settings.service_name,
    service=_settings.service_name,
)

_tracer: Tracer | None = None


def get_tracer() -> Tracer:
    """Return the X-Ray tracer, constructing it on first use.

    Deferred because importing the X-Ray SDK is only worth its package weight in
    stages that actually trace. Callers should check `tracing_enabled()` first.
    """
    global _tracer
    if _tracer is None:
        from aws_lambda_powertools import Tracer

        _tracer = Tracer(service=_settings.service_name)
    return _tracer


def tracing_enabled() -> bool:
    """Whether this stage traces at all. Local development never does."""
    return _settings.stage != "local"


FREE_TIER_CUSTOM_METRICS = 10
"""CloudWatch's always-free allowance, in **billed series** — not in metric names.

Each unique name + dimension-value combination is billed separately, and there is no way
to buy back into the free tier once it is exceeded.
"""

METRIC_CARDINALITY: dict[str, int] = {
    # `outcome` takes one of the four effect names, so each of these expands to four
    # billed series rather than one.
    "decisions": 4,
    "dry_run.decisions": 4,
    # One `stage` per deployment, and a stage deploys its own account-independent series.
    "evaluate.latency_ms": 1,
    "policy.version": 1,
}
"""Every metric this service emits, and how many billed series each becomes.

**This budget is full: 4 + 4 + 1 + 1 = 10 of 10.** Adding any metric now costs money, so
a new one must displace an existing one. Anything finer-grained — per rule, per tenant,
per agent — belongs in a structured log line and is queried through Logs Insights, which
is covered by the far larger 5 GB log allowance.

The arithmetic here was wrong in the original plan, which counted `dry_run.decisions` as
one series and reserved slots for `fail_closed_events`, `hitl.queue_age_seconds`, and
`errors`. `dry_run.decisions` carries an `outcome` dimension, so it is four; and the
three reserved names are not emitted by any code path. Implementing them as written would
take the total to 13 and start billing — which is exactly the kind of thing that is
discovered on an invoice rather than in review, so
`test_metric_cardinality_stays_inside_the_free_tier` now asserts it against the metrics
actually emitted.
"""

METRIC_BUDGET: frozenset[str] = frozenset(METRIC_CARDINALITY)
"""The permitted metric names. Kept as a name set for callers that only need membership."""
