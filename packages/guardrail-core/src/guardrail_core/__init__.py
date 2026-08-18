"""Guardrail policy engine core.

This package is deliberately pure: no network calls, no AWS SDK, no filesystem
access in the evaluation path. Everything here is a function of its inputs, which
is what makes policy decisions reproducible, testable, and auditable.

The evaluation engine itself lands in M1. M0 establishes the shared vocabulary.
"""

from guardrail_core.effects import Effect

__all__ = ["Effect", "__version__"]

__version__ = "0.1.0"
