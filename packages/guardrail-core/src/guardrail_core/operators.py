"""The closed set of predicate operators, and UNKNOWN semantics.

**Why not `eval()`.** Policy files are untrusted input: they arrive from a git repo, an
API call, or an object store. Evaluating them as code would hand arbitrary execution to
anyone who can edit a policy -- in the one service whose entire job is preventing
unauthorized actions. A fixed operator table is also auditable: a reviewer can enumerate
everything a rule is capable of doing by reading this file.

**UNKNOWN.** Extractors cannot always determine a value. "How many rows does this
`DELETE ... WHERE` affect?" is not always answerable without running it. Rather than
guess, an extractor returns `UNKNOWN`, and a predicate over UNKNOWN resolves according to
which way is *safe* for the rule it belongs to -- see `resolve_unknown`.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable, Sequence
from typing import Any, Final

from guardrail_core.effects import Effect


class _Unknown:
    """Sentinel for a value an extractor could not determine.

    A singleton so `is UNKNOWN` works, and deliberately falsy-proof: it raises on
    `bool()` to stop code from silently treating "we don't know" as "no".
    """

    _instance: _Unknown | None = None

    def __new__(cls) -> _Unknown:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNKNOWN"

    def __bool__(self) -> bool:
        raise TypeError(
            "UNKNOWN has no truth value. Handle it explicitly -- treating 'we could not "
            "determine this' as False is how a guardrail silently stops guarding."
        )


UNKNOWN: Final = _Unknown()


class PolicyError(ValueError):
    """A policy bundle is malformed. Raised at load time, never at evaluation time."""


def _as_sequence(value: Any) -> Sequence[Any]:
    """Coerce a scalar to a one-element sequence so `any_*` operators are uniform."""
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def _numeric_compare(actual: Any, expected: Any, op: Callable[[Any, Any], bool]) -> bool:
    """Compare two values, refusing rather than guessing on mismatched types.

    Python would happily compare `"5" > 3`... by raising, but `True > 0` silently works.
    A policy that accidentally compares a bool to a threshold should fail loudly at
    evaluation, not quietly match.
    """
    if isinstance(actual, bool) or isinstance(expected, bool):
        return False
    if not isinstance(actual, (int, float)) or not isinstance(expected, (int, float)):
        return False
    return op(actual, expected)


def _op_eq(actual: Any, expected: Any) -> bool:
    return bool(actual == expected)


def _op_ne(actual: Any, expected: Any) -> bool:
    return bool(actual != expected)


def _op_gt(actual: Any, expected: Any) -> bool:
    return _numeric_compare(actual, expected, lambda a, b: a > b)


def _op_gte(actual: Any, expected: Any) -> bool:
    return _numeric_compare(actual, expected, lambda a, b: a >= b)


def _op_lt(actual: Any, expected: Any) -> bool:
    return _numeric_compare(actual, expected, lambda a, b: a < b)


def _op_lte(actual: Any, expected: Any) -> bool:
    return _numeric_compare(actual, expected, lambda a, b: a <= b)


def _op_in(actual: Any, expected: Any) -> bool:
    return actual in _as_sequence(expected)


def _op_not_in(actual: Any, expected: Any) -> bool:
    return actual not in _as_sequence(expected)


def _op_any_in(actual: Any, expected: Any) -> bool:
    """True when at least one element of `actual` appears in `expected`."""
    allowed = _as_sequence(expected)
    return any(item in allowed for item in _as_sequence(actual))


def _op_any_not_in(actual: Any, expected: Any) -> bool:
    """True when at least one element of `actual` is absent from `expected`.

    This is the "any recipient is external" operator. Note it is emphatically *not*
    `not any_in`: a message to one internal and one external address must match, and
    `not any_in` would miss it.
    """
    allowed = _as_sequence(expected)
    return any(item not in allowed for item in _as_sequence(actual))


def _op_contains(actual: Any, expected: Any) -> bool:
    if isinstance(actual, str) and isinstance(expected, str):
        return expected in actual
    return expected in _as_sequence(actual)


def _op_icontains(actual: Any, expected: Any) -> bool:
    """Case-insensitive substring match -- for paths like `/srv/Confidential/q3.pdf`."""
    if isinstance(actual, str) and isinstance(expected, str):
        return expected.casefold() in actual.casefold()
    return False


def _op_matches(actual: Any, expected: Any) -> bool:
    if not isinstance(actual, str) or not isinstance(expected, str):
        return False
    try:
        return re.search(expected, actual) is not None
    except re.error:
        # An invalid pattern is a policy authoring bug. Refusing to match is the safe
        # direction: it cannot grant permission, only fail to restrict, and the loader
        # rejects bad patterns up front anyway.
        return False


def _op_glob(actual: Any, expected: Any) -> bool:
    if not isinstance(actual, str) or not isinstance(expected, str):
        return False
    return fnmatch.fnmatchcase(actual, expected)


def _op_exists(actual: Any, expected: Any) -> bool:
    """Whether the path resolved to anything. `expected` is a bool."""
    present = actual is not None
    return present == bool(expected)


OPERATORS: Final[dict[str, Callable[[Any, Any], bool]]] = {
    "eq": _op_eq,
    "ne": _op_ne,
    "gt": _op_gt,
    "gte": _op_gte,
    "lt": _op_lt,
    "lte": _op_lte,
    "in": _op_in,
    "not_in": _op_not_in,
    "any_in": _op_any_in,
    "any_not_in": _op_any_not_in,
    "contains": _op_contains,
    "icontains": _op_icontains,
    "matches": _op_matches,
    "glob": _op_glob,
    "exists": _op_exists,
}
"""Every operation a policy rule can perform. Nothing else is reachable from a bundle."""


def resolve_unknown(effect: Effect) -> bool:
    """How a predicate over an UNKNOWN value resolves, given its rule's effect.

    The rule is "assume the outcome that restricts". For a restrictive rule, an unknown
    predicate is treated as **matching**: if we cannot tell how many rows a delete
    affects, we do not assume it is few. For a permissive rule, an unknown predicate is
    treated as **not matching**: permission is never granted on the strength of something
    we failed to determine.

    Both directions point the same way -- toward not letting an unmeasured action
    through.
    """
    return effect >= Effect.REQUIRE_HITL


def apply_operator(op_name: str, actual: Any, expected: Any, *, effect: Effect) -> bool:
    """Evaluate one predicate, honouring UNKNOWN.

    Raises PolicyError for an unknown operator. That can only happen if a bundle bypassed
    validation, so failing loudly is right -- silently skipping an unrecognised predicate
    would turn a typo into a disabled security rule.
    """
    if actual is UNKNOWN:
        return resolve_unknown(effect)

    operator = OPERATORS.get(op_name)
    if operator is None:
        valid = ", ".join(sorted(OPERATORS))
        raise PolicyError(f"unknown operator {op_name!r}; expected one of: {valid}")

    return operator(actual, expected)
