"""Runtime configuration.

Every value is supplied by the environment. On Lambda those come from CDK-managed
environment variables (themselves sourced from SSM Parameter Store); locally they
come from a git-ignored `.env`. There are no secrets in this file and none in the
repository — see `.env.example` for what each value means and where to get it.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, read once per process and cached."""

    model_config = SettingsConfigDict(
        env_prefix="GUARDRAIL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Identity -----------------------------------------------------------
    stage: Literal["local", "dev", "prod"] = "local"
    """Deployment stage. Drives log verbosity and which resources are addressed."""

    service_name: str = "guardrail"
    """Used as the Powertools service dimension and the CloudWatch namespace."""

    version: str = "0.0.0-dev"
    """Build identifier, set to the git SHA by CI so a running deploy is traceable."""

    # --- Behaviour ----------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    aws_region: str = "us-east-1"
    """CloudFront-associated resources must live in us-east-1."""

    # --- Policy lifecycle ---------------------------------------------------
    policy_bundle_id: str = "default"
    """Which bundle this deployment serves. One name per deployment, so activating a
    version can never accidentally switch which policy is in force."""

    rate_limit_per_minute: float = 600.0
    """Requests per minute per tenant, per warm container. Set to 0 to disable.

    Enforced in-process, so the global ceiling is this multiplied by the number of
    concurrent containers -- see `ratelimit.py`, which explains why a DynamoDB counter is
    the wrong answer at 5 provisioned WCU. Lambda reserved concurrency is the hard cap."""

    rate_limit_burst: float = 0.0
    """Bucket size. 0 means "one second's worth of `rate_limit_per_minute`"."""

    reserved_concurrency: int = 10
    """Lambda's hard global ceiling, and the multiplier on the per-container rate above.

    Also the backstop that keeps a runaway agent inside the free tier: 10 containers
    cannot outrun 5 provisioned WCU for long, and Lambda enforces it rather than this
    code."""

    policy_refresh_seconds: float = 30.0
    """How stale the active policy may be in a warm container.

    An explicit bound, not a guess. Lambda freezes containers between invocations, so
    a background poller cannot run and the only place to re-check is the request path.
    Thirty seconds makes an emergency rollback feel immediate while costing one small
    eventually-consistent read per container per half minute. Set to 0 to re-check on
    every request -- correct for a test, wasteful in production."""

    # --- Storage (created in M1; declared now so readiness can report honestly) ---
    audit_table_name: str | None = Field(default=None)
    decisions_table_name: str | None = Field(default=None)
    policies_table_name: str | None = Field(default=None)

    @property
    def is_lambda(self) -> bool:
        """True when running inside the Lambda runtime rather than a container."""
        import os

        return "AWS_LAMBDA_FUNCTION_NAME" in os.environ


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached because Lambda reuses a warm container across invocations; re-parsing the
    environment on every request would be wasted work on the hot path.
    """
    return Settings()
