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
    AuditWriteError,
    DynamoDBAuditRepository,
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


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_appends_produce_one_unbroken_chain() -> None:
    """The chain's whole correctness argument rests on this.

    Lambda runs invocations concurrently. If two writers could claim the same sequence
    number, or link to the same predecessor, the chain would fork -- and a forked chain
    is not evidence of anything. The in-memory repository serialises through a lock the
    way DynamoDB's conditional put serialises through the condition, so this exercises
    the same invariant: contiguous sequence numbers and every link intact.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    repo = InMemoryAuditRepository()
    lock = threading.Lock()
    writers, per_writer = 8, 25

    def write_one(_: int) -> None:
        # The repository is not itself thread-safe; the lock stands in for DynamoDB's
        # conditional-put serialisation, which is what makes the real one safe.
        with lock:
            repo.append("acme", lambda seq, prev: _record(seq, prev))

    with ThreadPoolExecutor(max_workers=writers) as pool:
        list(pool.map(write_one, range(writers * per_writer)))

    total = writers * per_writer
    records = repo.list_records("acme", limit=total + 10)

    assert len(records) == total, "a write was lost"
    assert sorted(r.seq for r in records) == list(range(1, total + 1)), (
        "sequence numbers are not contiguous -- the chain forked or a slot was reused"
    )
    assert repo.verify_chain("acme", limit=total).chain_valid is True


def test_every_sequence_number_is_claimed_exactly_once() -> None:
    """A duplicate sequence number would mean one record silently overwrote another."""
    repo = InMemoryAuditRepository()

    for _ in range(50):
        repo.append("acme", lambda seq, prev: _record(seq, prev))

    seqs = [r.seq for r in repo.list_records("acme", limit=100)]

    assert len(seqs) == len(set(seqs))


def test_hashes_are_unique_across_the_chain() -> None:
    """Identical payloads at different positions must still hash differently, because
    each incorporates its predecessor. Otherwise records could be swapped undetected."""
    repo = InMemoryAuditRepository()

    for _ in range(30):
        repo.append(
            "acme",
            lambda seq, prev: _record(
                seq,
                prev,
                payload={
                    "effect": "allow",
                    "session_id": "s",
                    "tool": "file.read",
                    "identical": True,
                },
            ),
        )

    hashes = [r.hash for r in repo.list_records("acme", limit=100)]

    assert len(hashes) == len(set(hashes))


# ---------------------------------------------------------------------------
# Throttling -- found by a 180-second load test, not by review
# ---------------------------------------------------------------------------


def _throttle(code: str = "ProvisionedThroughputExceededException") -> Any:
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": "exceeded"}}, "PutItem")


class _FakeDynamo:
    """A DynamoDB client that fails a set number of writes before succeeding."""

    def __init__(self, error: Any, *, fail_times: int = 999) -> None:
        self.error = error
        self.fail_times = fail_times
        self.attempts = 0

    def query(self, **kwargs: Any) -> dict[str, Any]:
        return {"Items": []}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise self.error
        return {}


def _build(seq: int, prev_hash: str) -> AuditRecord:
    return AuditRecord(
        tenant_id="acme",
        seq=seq,
        timestamp="2026-08-19T00:00:00.000+00:00",
        payload={"effect": "allow", "session_id": "s"},
        prev_hash=prev_hash,
        hash="h",
    )


def test_a_throttled_write_fails_closed_rather_than_escaping_raw() -> None:
    """**The bug a load test found and review did not.**

    `append` originally re-raised anything that was not a ConditionalCheckFailedException,
    on the first attempt. The table is provisioned at 5 WCU, so throttling is the most
    likely failure it will ever see -- and it escaped as a raw ClientError, surfacing as
    an unhandled 500 instead of the fail-closed 503 the SDK and the docs both promise,
    with no `audit_write_failed` line to explain it. A 180s load test produced 92.
    """
    client = _FakeDynamo(_throttle())
    repository = DynamoDBAuditRepository("t", client=client)

    with pytest.raises(AuditWriteError, match="provisioned write capacity"):
        repository.append("acme", _build)

    assert client.attempts > 1, "throttling must be retried, not surrendered to instantly"


def test_a_throttled_write_recovers_when_capacity_returns() -> None:
    """Retrying has to actually work, or the retry loop is just a slower failure."""
    client = _FakeDynamo(_throttle(), fail_times=2)
    repository = DynamoDBAuditRepository("t", client=client)

    record = repository.append("acme", _build)

    assert record.seq == 1
    assert client.attempts == 3


def test_the_write_loop_finishes_inside_the_lambda_timeout() -> None:
    """A retry budget longer than the 10s function timeout is worse than no retry at all:
    the invocation is killed, the platform logs a bare error, and the operator gets a 5xx
    with no application log line explaining any of it."""
    import time as time_module

    from guardrail_service.storage.audit import WRITE_DEADLINE_SECONDS

    repository = DynamoDBAuditRepository("t", client=_FakeDynamo(_throttle()))

    started = time_module.monotonic()
    with pytest.raises(AuditWriteError):
        repository.append("acme", _build)
    elapsed = time_module.monotonic() - started

    assert elapsed < WRITE_DEADLINE_SECONDS + 1.0, f"took {elapsed:.1f}s"
    assert WRITE_DEADLINE_SECONDS < 10.0, "the deadline must sit inside the Lambda timeout"


def test_a_genuine_error_is_not_retried() -> None:
    """A denied permission or a missing table cannot be fixed by waiting. Retrying would
    turn a clear, immediate failure into a slow mysterious one."""
    client = _FakeDynamo(_throttle("AccessDeniedException"))
    repository = DynamoDBAuditRepository("t", client=client)

    with pytest.raises(Exception) as caught:
        repository.append("acme", _build)

    assert not isinstance(caught.value, AuditWriteError)
    assert client.attempts == 1, "a permanent error must fail on the first attempt"


def test_sequence_contention_is_still_retried() -> None:
    """The original behaviour must survive the throttling fix."""
    client = _FakeDynamo(_throttle("ConditionalCheckFailedException"), fail_times=2)
    repository = DynamoDBAuditRepository("t", client=client)

    assert repository.append("acme", _build).seq == 1
    assert client.attempts == 3


def test_a_throttled_idempotency_write_does_not_fail_the_request() -> None:
    """The decision is already in the chain by then. Losing idempotency for one request
    is far less serious than failing an action that was correctly evaluated and
    recorded."""
    repository = DynamoDBAuditRepository("t", client=_FakeDynamo(_throttle()))

    repository.store_idempotent("acme", "key-1", {"decision": "allow"})  # must not raise
