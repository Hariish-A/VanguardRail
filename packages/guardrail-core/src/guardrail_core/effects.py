"""The four outcomes a policy evaluation can produce.

Ordering matters: `Effect` is comparable, and a higher value is more restrictive.
That is what lets the engine resolve several matching rules by taking the strictest
one (`max(...)`) rather than depending on rule order in the YAML file — under
first-match-wins, reordering a policy file silently weakens security.
"""

from __future__ import annotations

from enum import IntEnum


class Effect(IntEnum):
    """A policy outcome, ordered from most permissive to most restrictive."""

    ALLOW = 0
    """Execute the tool call. Recorded in the audit log like everything else."""

    LOG_AND_ALLOW = 1
    """Execute, but flag the record as noteworthy for review."""

    REQUIRE_HITL = 2
    """Suspend execution until a human approves or denies."""

    BLOCK = 3
    """Reject the tool call. It is never dispatched."""

    @property
    def wire_name(self) -> str:
        """The lowercase name used in policy YAML and API responses."""
        return self.name.lower()

    @classmethod
    def from_wire(cls, value: str) -> Effect:
        """Parse an effect from policy YAML or an API payload."""
        try:
            return cls[value.strip().upper()]
        except KeyError:
            valid = ", ".join(e.wire_name for e in cls)
            raise ValueError(f"unknown effect {value!r}; expected one of: {valid}") from None

    @classmethod
    def most_restrictive(cls, effects: object) -> Effect:
        """Resolve several matching rules to the strictest outcome.

        Returns ALLOW when nothing matched, which the caller may override with the
        bundle's configured default effect.
        """
        if not isinstance(effects, (list, tuple, set, frozenset)):
            raise TypeError("effects must be a collection of Effect values")
        return max(effects, default=cls.ALLOW)
