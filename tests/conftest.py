"""Shared test configuration.

**Rate limiting is disabled for the suite by default.**

Most suites here fire dozens of requests at the app as fast as the process allows, which
is nothing like real traffic and would exhaust a per-tenant token bucket several times
over. Leaving the limiter on would make unrelated suites fail intermittently with 429s,
and the natural response to that -- raising the limit until the noise stops -- would
quietly turn the limiter into a no-op in production too.

So it is switched off here explicitly, and the tests that are actually *about* rate
limiting turn it back on with the settings they need. That keeps the control real and the
failures meaningful.

Set at import time rather than in a fixture because `Settings` is an `lru_cache`d
singleton: by the time a fixture runs, something may already have read it.
"""

from __future__ import annotations

import os

os.environ.setdefault("GUARDRAIL_RATE_LIMIT_PER_MINUTE", "0")
