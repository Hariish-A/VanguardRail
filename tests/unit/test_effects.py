"""Effect ordering — the foundation of conflict resolution between policy rules."""

from __future__ import annotations

import pytest
from guardrail_core import Effect


def test_effects_are_ordered_by_restrictiveness() -> None:
    assert Effect.ALLOW < Effect.LOG_AND_ALLOW < Effect.REQUIRE_HITL < Effect.BLOCK


def test_most_restrictive_wins_regardless_of_order() -> None:
    """Rule order in the YAML file must never change the outcome. Under
    first-match-wins, moving a rule up or down silently weakens security."""
    effects = [Effect.ALLOW, Effect.BLOCK, Effect.LOG_AND_ALLOW]

    assert Effect.most_restrictive(effects) == Effect.BLOCK
    assert Effect.most_restrictive(list(reversed(effects))) == Effect.BLOCK


def test_no_matching_rules_resolves_to_allow() -> None:
    """The caller then applies the bundle's configured default effect."""
    assert Effect.most_restrictive([]) == Effect.ALLOW


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        ("block", Effect.BLOCK),
        ("BLOCK", Effect.BLOCK),
        ("  require_hitl  ", Effect.REQUIRE_HITL),
        ("log_and_allow", Effect.LOG_AND_ALLOW),
        ("allow", Effect.ALLOW),
    ],
)
def test_wire_names_round_trip(wire: str, expected: Effect) -> None:
    assert Effect.from_wire(wire) == expected
    assert Effect.from_wire(expected.wire_name) == expected


def test_unknown_effect_is_rejected_with_a_useful_message() -> None:
    """A typo in a policy file must fail loudly at load time, not silently permit."""
    with pytest.raises(ValueError, match="unknown effect"):
        Effect.from_wire("deny")
