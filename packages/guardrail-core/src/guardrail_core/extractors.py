"""Derived facts.

Policies should not be written against raw arguments, because the same intent arrives in
many shapes. "How many records does this delete affect?" might come from an explicit
`count`, a list of ids, or a SQL `WHERE` clause. An extractor normalizes those into one
fact, so a policy author writes one rule instead of five:

    args.count = 500                   ─┐
    args.ids = [1, 2, ... 500]          ├─► derived.record_count = 500
    args.where = "last_login < ..."    ─┘   (or UNKNOWN)

Extractors are **pure and conservative**. When a value cannot be determined they return
UNKNOWN rather than guessing, and `operators.resolve_unknown` then resolves the predicate
in whichever direction restricts. Guessing "probably small" is how a guardrail silently
stops guarding.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, Final

from guardrail_core.operators import UNKNOWN

Extractor = Callable[[Mapping[str, Any]], Any]

# ---------------------------------------------------------------------------
# Record count
# ---------------------------------------------------------------------------

_COUNT_KEYS: Final = ("count", "record_count", "limit", "num_records", "n")
_ID_LIST_KEYS: Final = ("ids", "record_ids", "keys", "rows", "items", "documents")


def extract_record_count(arguments: Mapping[str, Any]) -> Any:
    """Blast radius of a mutation, as a row count.

    Resolution order, most reliable first:

    1. an explicit numeric count argument
    2. the length of an id/row collection
    3. UNKNOWN

    A SQL `WHERE` clause deliberately yields **UNKNOWN** rather than a guess: the row
    count depends on data the engine cannot see, and the engine must never pretend to
    know. Combined with fail-closed UNKNOWN handling, `DELETE ... WHERE <anything>` trips
    a bulk-delete rule -- which is the correct default for an unmeasurable deletion.
    """
    for key in _COUNT_KEYS:
        value = arguments.get(key)
        if isinstance(value, bool):
            continue  # bool is an int subclass; a flag is not a count
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)

    for key in _ID_LIST_KEYS:
        value = arguments.get(key)
        if isinstance(value, (list, tuple, set)):
            return len(value)

    # An unbounded filter is present but its cardinality is unknowable from here.
    if any(key in arguments for key in ("where", "filter", "query", "criteria")):
        return UNKNOWN

    return UNKNOWN


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

_RECIPIENT_KEYS: Final = ("to", "recipients", "cc", "bcc", "email", "address")
_EMAIL_PATTERN: Final = re.compile(r"[^@\s,;]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def _iter_recipient_strings(arguments: Mapping[str, Any]) -> list[str]:
    """Collect every recipient across all address fields.

    `cc` and `bcc` are included on purpose. A rule that only inspected `to` would let a
    message reach an external party via `bcc` without ever matching -- exactly the kind
    of gap this project exists to close.
    """
    found: list[str] = []
    for key in _RECIPIENT_KEYS:
        value = arguments.get(key)
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, (list, tuple, set)):
            found.extend(item for item in value if isinstance(item, str))
    return found


def extract_recipient_domains(arguments: Mapping[str, Any]) -> Any:
    """Lowercased domains of every recipient, or UNKNOWN if none can be parsed."""
    domains: list[str] = []
    for raw in _iter_recipient_strings(arguments):
        for match in _EMAIL_PATTERN.finditer(raw):
            domains.append(match.group(1).casefold())

    if not domains:
        # A send with recipients we cannot parse is more suspicious than one with none,
        # so an unparseable address must not read as "no external recipients".
        return UNKNOWN if _iter_recipient_strings(arguments) else []

    # Deduplicated but order-stable, so audit records and messages read predictably.
    return list(dict.fromkeys(domains))


def extract_recipient_count(arguments: Mapping[str, Any]) -> Any:
    """How many addresses a message is going to -- a blast-radius signal of its own."""
    recipients = _iter_recipient_strings(arguments)
    if not recipients:
        return 0
    return sum(len(_EMAIL_PATTERN.findall(raw)) for raw in recipients)


# ---------------------------------------------------------------------------
# Filesystem / resource paths
# ---------------------------------------------------------------------------

_PATH_KEYS: Final = ("path", "file", "filename", "filepath", "key", "uri", "url")


def extract_path(arguments: Mapping[str, Any]) -> Any:
    """The resource path being touched, normalized to forward slashes."""
    for key in _PATH_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value.replace("\\", "/")
    return UNKNOWN


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

EXTRACTORS: Final[dict[str, Extractor]] = {
    "record_count": extract_record_count,
    "recipient_domains": extract_recipient_domains,
    "recipient_count": extract_recipient_count,
    "path": extract_path,
}
"""Available `derived.*` facts. A policy referencing anything else fails validation."""


def derive_facts(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Run every extractor over one call's arguments.

    All of them run rather than only those a rule mentions: the set is tiny and pure, and
    recording every derived fact in the audit log is what lets someone later ask "what
    did the engine believe when it decided this?"
    """
    return {name: extractor(arguments) for name, extractor in EXTRACTORS.items()}
