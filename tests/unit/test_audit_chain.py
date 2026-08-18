"""Hash chain correctness.

Two failure modes are covered, and the second is the subtle one:

* **Tampering must be detected** -- edits, deletions, and reorderings each produce a
  distinguishable failure.
* **Honest records must never be reported as tampered.** A false alarm is arguably worse
  than a missed one: it destroys trust in the log, and it fires during an audit rather
  than during a test. The round-trip tests below exist because storing floats in DynamoDB
  originally raised `TypeError: Float types are not supported`, and the obvious fix
  (convert to Decimal and back) risked exactly this.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from guardrail_service.storage.audit import (
    GENESIS_HASH,
    AuditRecord,
    InMemoryAuditRepository,
    canonical_json,
    compute_hash,
    verify_records,
)


def _record(seq: int, prev_hash: str, *, payload: dict[str, Any] | None = None) -> AuditRecord:
    body = payload or {
        "effect": "allow",
        "session_id": "s1",
        "tool": "file.read",
        "n": seq,
    }
    return AuditRecord(
        tenant_id="acme",
        seq=seq,
        timestamp=f"2026-08-18T10:00:{seq:02d}.000+00:00",
        payload=body,
        prev_hash=prev_hash,
        hash=compute_hash(prev_hash, body),
    )


def _chain(length: int) -> list[AuditRecord]:
    records: list[AuditRecord] = []
    prev = GENESIS_HASH
    for seq in range(1, length + 1):
        record = _record(seq, prev)
        records.append(record)
        prev = record.hash
    return records


# ---------------------------------------------------------------------------
# Round-trip fidelity -- no false alarms
# ---------------------------------------------------------------------------


def test_payload_survives_the_dynamodb_item_round_trip() -> None:
    """Write then read must reproduce the payload exactly, or verification lies."""
    record = _record(1, GENESIS_HASH)

    restored = AuditRecord.from_item(record.to_item())

    assert restored.payload == record.payload
    assert restored.hash == record.hash
    assert verify_records([restored]).chain_valid is True


@pytest.mark.parametrize(
    "value",
    [
        1000.50,  # a refund amount -- the case that originally broke storage
        0.1,  # not exactly representable in binary floating point
        1e-7,
        -273.15,
        3.141592653589793,
        10**18,  # beyond a 64-bit float's integer precision
        "unicode: café 日本語 🎯",
        {"nested": {"deep": [1.5, 2.5, {"deeper": 0.3}]}},
        [True, False, None],
        "",
    ],
)
def test_awkward_values_round_trip_without_false_tampering(value: Any) -> None:
    """Floats above all. DynamoDB rejects them outright, and a lossy workaround would
    recompute a different hash and report an untouched record as modified."""
    payload = {"effect": "allow", "session_id": "s", "arguments": {"v": value}}
    record = _record(1, GENESIS_HASH, payload=payload)

    restored = AuditRecord.from_item(record.to_item())

    assert restored.payload == payload
    assert verify_records([restored]).chain_valid is True


def test_canonical_json_is_stable_across_key_insertion_order() -> None:
    """Two dicts with the same content must hash identically however they were built."""
    first = {"b": 1, "a": {"y": 2, "x": 3}}
    second = {"a": {"x": 3, "y": 2}, "b": 1}

    assert canonical_json(first) == canonical_json(second)
    assert compute_hash("prev", first) == compute_hash("prev", second)


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------


def test_intact_chain_verifies() -> None:
    result = verify_records(_chain(10))

    assert result.chain_valid is True
    assert result.records_checked == 10
    assert result.broken_at_seq is None


def test_empty_chain_verifies() -> None:
    """Nothing written means nothing tampered with."""
    assert verify_records([]).chain_valid is True


def test_edited_payload_is_detected() -> None:
    """The realistic insider case: change one record and leave everything else alone."""
    chain = _chain(5)
    tampered = AuditRecord(
        tenant_id=chain[2].tenant_id,
        seq=chain[2].seq,
        timestamp=chain[2].timestamp,
        payload={**chain[2].payload, "effect": "allow", "tool": "totally.innocent"},
        prev_hash=chain[2].prev_hash,
        hash=chain[2].hash,  # left untouched, as an attacker would
    )
    chain[2] = tampered

    result = verify_records(chain)

    assert result.chain_valid is False
    assert result.broken_at_seq == 3
    assert result.reason is not None and "content mismatch" in result.reason


def test_recomputing_the_hash_after_editing_still_breaks_the_chain() -> None:
    """A thorough attacker recomputes the edited record's hash. The next record's
    prev_hash then no longer matches -- which is the entire point of chaining."""
    chain = _chain(5)
    # The base payload is already "allow", so flip to a value that genuinely differs --
    # otherwise the "edit" is a no-op and the test proves nothing.
    edited_payload = {**chain[2].payload, "effect": "block", "tool": "db.drop_table"}
    chain[2] = AuditRecord(
        tenant_id="acme",
        seq=3,
        timestamp=chain[2].timestamp,
        payload=edited_payload,
        prev_hash=chain[2].prev_hash,
        hash=compute_hash(chain[2].prev_hash, edited_payload),
    )

    result = verify_records(chain)

    assert result.chain_valid is False
    assert result.broken_at_seq == 4
    assert result.reason is not None and "broken link" in result.reason


def test_deleted_record_is_detected_as_a_gap() -> None:
    chain = _chain(5)
    del chain[2]

    result = verify_records(chain)

    assert result.chain_valid is False
    assert result.broken_at_seq == 4
    assert result.reason is not None and "gap" in result.reason


def test_reordered_records_are_detected() -> None:
    chain = _chain(5)
    chain[1], chain[3] = chain[3], chain[1]

    assert verify_records(chain).chain_valid is False


def test_first_record_must_chain_to_genesis() -> None:
    """A chain that starts mid-air suggests earlier records were removed wholesale."""
    forged = _record(1, "f" * 64)

    result = verify_records([forged])

    assert result.chain_valid is False


# ---------------------------------------------------------------------------
# Repository behaviour
# ---------------------------------------------------------------------------


def test_repository_assigns_contiguous_sequence_numbers() -> None:
    repo = InMemoryAuditRepository()

    for _ in range(10):
        repo.append("acme", lambda seq, prev: _record(seq, prev))

    assert repo.verify_chain("acme").chain_valid is True
    assert [r.seq for r in reversed(repo.list_records("acme", limit=100))] == list(range(1, 11))


def test_tenants_have_independent_chains() -> None:
    """One tenant's activity must not appear in, or perturb, another's chain."""
    repo = InMemoryAuditRepository()

    repo.append("acme", lambda seq, prev: _record(seq, prev))
    repo.append("globex", lambda seq, prev: _record(seq, prev))
    repo.append("acme", lambda seq, prev: _record(seq, prev))

    assert len(repo.list_records("acme")) == 2
    assert len(repo.list_records("globex")) == 1
    assert repo.verify_chain("acme").chain_valid is True
    assert repo.verify_chain("globex").chain_valid is True


def test_item_shape_contains_no_floats() -> None:
    """DynamoDB rejects float attributes outright, so the item must not contain any."""
    record = _record(1, GENESIS_HASH, payload={"effect": "allow", "session_id": "s", "f": 1.5})

    item = record.to_item()

    def has_float(value: Any) -> bool:
        if isinstance(value, float):
            return True
        if isinstance(value, dict):
            return any(has_float(v) for v in value.values())
        if isinstance(value, list):
            return any(has_float(v) for v in value)
        return False

    assert not has_float(item), f"float found in DynamoDB item: {item}"
    # The float lives inside the JSON string, which is exactly where it is safe.
    assert json.loads(item["payload_json"])["f"] == 1.5
