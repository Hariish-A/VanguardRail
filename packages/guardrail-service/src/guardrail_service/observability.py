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


METRIC_BUDGET: frozenset[str] = frozenset(
    {
        "decisions",  # x4 outcome dimensions = 4 billed metrics
        "evaluate.latency_ms",
        "fail_closed_events",
        "hitl.queue_age_seconds",
        "policy.version",
        "errors",
        "dry_run.decisions",
    }
)
"""The complete set of permitted metric names — 10 billed metrics once dimensions expand.

CloudWatch's free tier is 10 custom metrics in total and there is no way to buy back
into it, so any new metric must displace an existing one. Anything finer-grained
belongs in a structured log line and is queried through Logs Insights.
"""
