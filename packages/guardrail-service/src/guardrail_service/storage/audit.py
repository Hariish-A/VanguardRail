"""The tamper-evident audit log.

Every evaluated action produces a record -- allows included. An audit log that only
records denials cannot answer "what did this agent do last Tuesday", which is the
question compliance actually asks.

## The hash chain

Each record carries the hash of its predecessor:

    record[n].hash = sha256(record[n-1].hash || canonical_json(record[n]))

Altering or deleting record *n* breaks every hash from *n* onward, and `verify_chain`
walks the chain to detect it.

This is deliberately **tamper-evident, not tamper-proof**. Anyone with sufficient IAM
permissions can still delete the table. What they cannot do is quietly change one record
and leave the log internally consistent -- which is the realistic insider scenario.

## Concurrency

A hash chain needs a total order, and Lambda runs invocations concurrently. Two writers
could read the same head and fork the chain.

The approach here is read-latest plus a conditional put:

1. Query the tenant's most recent record (strongly consistent, base table).
2. Compute the new record's hash from that predecessor.
3. `PutItem` at `seq + 1` with `attribute_not_exists(sk)`.
4. On a condition failure another writer won that slot -- re-read and retry.

Exactly one writer can hold each sequence number, so the chain stays linear. The
alternative -- a head pointer updated inside a transaction -- was rejected because
DynamoDB transactions consume double the write capacity, and the free tier allows only
25 WCU across the whole account.

**Failure is fail-closed.** If the audit write cannot be completed, the evaluation fails
rather than returning a decision. Permitting an action we could not record is precisely
the gap this system exists to close.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from guardrail_core.models import ActionEnvelope, EvaluationResult
from guardrail_core.operators import UNKNOWN

GENESIS_HASH = "0" * 64
"""Predecessor hash of the first record in a tenant's chain."""

MAX_WRITE_ATTEMPTS = 6
"""Bounded retries on sequence contention before failing closed."""


class AuditWriteError(RuntimeError):
    """The audit record could not be persisted, so no decision may be returned."""


def _json_safe(value: Any) -> Any:
    """Convert engine values into something JSON can represent.

    UNKNOWN becomes the string "UNKNOWN" so the audit record preserves the distinction
    between "we could not determine this" and "this was absent" -- which is often the
    reason a decision came out the way it did.
    """
    if value is UNKNOWN:
        return "UNKNOWN"
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def canonical_json(payload: dict[str, Any]) -> str:
    """Serialize deterministically, so the same record always hashes identically.

    Sorted keys and no incidental whitespace: without both, a record could re-serialize
    differently on another runtime and appear tampered with when it is not.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_hash(previous_hash: str, payload: dict[str, Any]) -> str:
    """Link one record to its predecessor."""
    material = f"{previous_hash}{canonical_json(payload)}".encode()
    return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True)
class AuditRecord:
    """One evaluated action, as stored.

    `payload` is what gets hashed; the surrounding fields are the chain metadata.
    """

    tenant_id: str
    seq: int
    timestamp: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str = field(default="")

    @staticmethod
    def build(
        *,
        tenant_id: str,
        seq: int,
        prev_hash: str,
        envelope: ActionEnvelope,
        result: EvaluationResult,
        facts: dict[str, Any],
        decision_id: str,
        latency_ms: float,
        request_id: str,
        timestamp: str | None = None,
    ) -> AuditRecord:
        """Assemble and hash a record.

        The payload deliberately includes the derived facts and the bundle version. A
        decision cannot be explained months later from the arguments alone -- a reviewer
        needs to know what the engine believed and which policy was in force.
        """
        ts = timestamp or datetime.now(UTC).isoformat(timespec="milliseconds")

        payload: dict[str, Any] = {
            "decision_id": decision_id,
            "request_id": request_id,
            "timestamp": ts,
            "tenant_id": tenant_id,
            "agent_id": envelope.agent_id,
            "session_id": envelope.session_id,
            "tool": envelope.tool,
            "arguments": _json_safe(envelope.arguments),
            "principal": envelope.principal.model_dump() if envelope.principal else None,
            "context": _json_safe(envelope.context),
            "derived": _json_safe(facts.get("derived", {})),
            "dry_run": envelope.dry_run,
            "effect": result.effect.wire_name,
            "matched_rules": [
                {
                    "rule_id": m.rule_id,
                    "effect": m.effect.wire_name,
                    "severity": m.severity,
                }
                for m in result.matched_rules
            ],
            "message": result.message,
            "bundle_id": result.bundle_id,
            "bundle_version": result.bundle_version,
            "unknown_paths": result.unknown_paths,
            "latency_ms": round(latency_ms, 3),
        }

        return AuditRecord(
            tenant_id=tenant_id,
            seq=seq,
            timestamp=ts,
            payload=payload,
            prev_hash=prev_hash,
            hash=compute_hash(prev_hash, payload),
        )

    def to_item(self) -> dict[str, Any]:
        """DynamoDB item shape.

        **The payload is stored as its canonical JSON string, not as a map.** Three
        reasons, and the first is a correctness requirement rather than a preference:

        1. DynamoDB cannot store Python floats at all (`TypeError: Float types are not
           supported`), and tool-call arguments legitimately contain them -- a refund
           amount of 1000.50, for instance. Converting to Decimal and back risks the
           payload not round-tripping byte-identically, which would recompute a
           different hash and raise a **false tamper alarm**. Storing the exact string
           that was hashed removes that entire class of bug.
        2. Verification then compares like with like: the bytes hashed at write time are
           the bytes read back.
        3. Nothing queries inside the payload -- the indexes are built from scalar
           attributes lifted out alongside it.

        Keys are zero-padded so lexicographic sort-key ordering matches numeric ordering;
        without padding, seq 10 would sort before seq 9.
        """
        return {
            "pk": f"TENANT#{self.tenant_id}",
            "sk": f"SEQ#{self.seq:012d}",
            "seq": self.seq,
            "timestamp": self.timestamp,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
            "payload_json": canonical_json(self.payload),
            # Outcome-by-time index, for "show me everything blocked this week".
            "gsi1pk": f"TENANT#{self.tenant_id}#OUTCOME#{self.payload['effect']}",
            "gsi1sk": f"{self.timestamp}#{self.seq:012d}",
            # Session-by-time index, for "what did this agent run do?".
            "gsi2pk": f"TENANT#{self.tenant_id}#SESSION#{self.payload['session_id']}",
            "gsi2sk": f"{self.timestamp}#{self.seq:012d}",
        }

    @staticmethod
    def from_item(item: dict[str, Any]) -> AuditRecord:
        return AuditRecord(
            tenant_id=str(item["pk"]).removeprefix("TENANT#"),
            seq=int(item["seq"]),
            timestamp=str(item["timestamp"]),
            payload=json.loads(item["payload_json"]),
            prev_hash=str(item["prev_hash"]),
            hash=str(item["hash"]),
        )


@dataclass(frozen=True)
class ChainVerification:
    """Result of walking a tenant's chain."""

    chain_valid: bool
    records_checked: int
    tenant_id: str
    broken_at_seq: int | None = None
    reason: str | None = None


def verify_records(records: list[AuditRecord]) -> ChainVerification:
    """Verify a contiguous run of records, oldest first.

    Pure, so it is testable without AWS and reusable by any storage backend.

    Three distinct failures are detected and reported separately, because they mean
    different things: a **gap** suggests a deleted record, a **broken link** suggests a
    reordered or substituted one, and a **content mismatch** suggests an edited one.
    """
    tenant = records[0].tenant_id if records else "unknown"

    if not records:
        return ChainVerification(chain_valid=True, records_checked=0, tenant_id=tenant)

    expected_prev = GENESIS_HASH if records[0].seq == 1 else records[0].prev_hash
    previous_seq: int | None = None

    for record in records:
        if previous_seq is not None and record.seq != previous_seq + 1:
            return ChainVerification(
                chain_valid=False,
                records_checked=len(records),
                tenant_id=tenant,
                broken_at_seq=record.seq,
                reason=(
                    f"sequence gap: record {previous_seq} is followed by {record.seq}; "
                    "a record appears to have been deleted"
                ),
            )

        if record.prev_hash != expected_prev:
            return ChainVerification(
                chain_valid=False,
                records_checked=len(records),
                tenant_id=tenant,
                broken_at_seq=record.seq,
                reason=(
                    f"broken link at seq {record.seq}: prev_hash does not match the "
                    "preceding record's hash"
                ),
            )

        recomputed = compute_hash(record.prev_hash, record.payload)
        if recomputed != record.hash:
            return ChainVerification(
                chain_valid=False,
                records_checked=len(records),
                tenant_id=tenant,
                broken_at_seq=record.seq,
                reason=(
                    f"content mismatch at seq {record.seq}: the stored hash does not "
                    "match the record body, so the record was modified after writing"
                ),
            )

        expected_prev = record.hash
        previous_seq = record.seq

    return ChainVerification(chain_valid=True, records_checked=len(records), tenant_id=tenant)


RecordBuilder = Callable[[int, str], AuditRecord]
"""Builds a record once its sequence number and predecessor hash are known.

The repository owns sequencing, so the caller supplies a factory rather than a finished
record -- a record's hash cannot be computed until its place in the chain is settled.
"""


class AuditRepository(Protocol):
    """Storage interface, so the API can be tested without AWS."""

    def append(self, tenant_id: str, build: RecordBuilder) -> AuditRecord: ...

    def list_records(
        self, tenant_id: str, *, limit: int = 50, effect: str | None = None
    ) -> list[AuditRecord]: ...

    def verify_chain(self, tenant_id: str, *, limit: int = 1000) -> ChainVerification: ...


class InMemoryAuditRepository:
    """Reference implementation used by tests and local development.

    Shares the chaining and verification logic with the DynamoDB repository, so a test
    passing here exercises the same invariants that run in production.
    """

    def __init__(self) -> None:
        self._records: dict[str, list[AuditRecord]] = {}

    def append(self, tenant_id: str, build: RecordBuilder) -> AuditRecord:
        chain = self._records.setdefault(tenant_id, [])
        seq = len(chain) + 1
        prev_hash = chain[-1].hash if chain else GENESIS_HASH

        record = build(seq, prev_hash)
        chain.append(record)
        return record

    def list_records(
        self, tenant_id: str, *, limit: int = 50, effect: str | None = None
    ) -> list[AuditRecord]:
        chain = self._records.get(tenant_id, [])
        if effect:
            chain = [r for r in chain if r.payload.get("effect") == effect]
        return list(reversed(chain))[:limit]

    def verify_chain(self, tenant_id: str, *, limit: int = 1000) -> ChainVerification:
        return verify_records(self._records.get(tenant_id, [])[:limit])


class DynamoDBAuditRepository:
    """DynamoDB-backed audit log.

    See the module docstring for why writes use read-latest plus a conditional put rather
    than a transaction: transactions cost double the write capacity, and the account's
    entire free allowance is 25 WCU.
    """

    def __init__(self, table_name: str, *, client: Any = None) -> None:
        self._table_name = table_name
        self._client = client

    @property
    def client(self) -> Any:
        """Lazily created, so importing this module needs no AWS credentials."""
        if self._client is None:
            import boto3

            self._client = boto3.client("dynamodb")
        return self._client

    # -- serialization -------------------------------------------------
    @staticmethod
    def _serialize(item: dict[str, Any]) -> dict[str, Any]:
        from boto3.dynamodb.types import TypeSerializer

        serializer = TypeSerializer()
        return {k: serializer.serialize(v) for k, v in item.items()}

    @staticmethod
    def _deserialize(item: dict[str, Any]) -> dict[str, Any]:
        from boto3.dynamodb.types import TypeDeserializer

        deserializer = TypeDeserializer()
        return {k: deserializer.deserialize(v) for k, v in item.items()}

    def _latest(self, tenant_id: str) -> AuditRecord | None:
        """Most recent record for a tenant, read strongly consistently.

        Consistency is required: an eventually consistent read could miss the record a
        concurrent writer just committed and fork the chain.
        """
        response = self.client.query(
            TableName=self._table_name,
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": f"TENANT#{tenant_id}"},
                ":prefix": {"S": "SEQ#"},
            },
            ScanIndexForward=False,
            Limit=1,
            ConsistentRead=True,
        )
        items = response.get("Items", [])
        if not items:
            return None
        return AuditRecord.from_item(self._deserialize(items[0]))

    def append(self, tenant_id: str, build: RecordBuilder) -> AuditRecord:
        """Append one record, retrying on sequence contention.

        Raises AuditWriteError once retries are exhausted. Callers must treat that as a
        failed evaluation: returning a decision we could not record would defeat the
        purpose of the system.
        """
        from botocore.exceptions import ClientError

        last_error: Exception | None = None

        for attempt in range(MAX_WRITE_ATTEMPTS):
            latest = self._latest(tenant_id)
            seq = (latest.seq + 1) if latest else 1
            prev_hash = latest.hash if latest else GENESIS_HASH

            record = build(seq, prev_hash)

            try:
                self.client.put_item(
                    TableName=self._table_name,
                    Item=self._serialize(record.to_item()),
                    ConditionExpression="attribute_not_exists(sk)",
                )
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
                # Another writer claimed this sequence number. Re-read and retry with
                # a short randomised backoff so contending writers do not resynchronise.
                last_error = exc
                time.sleep(0.02 * (2**attempt))
                continue
            else:
                return record

        raise AuditWriteError(
            f"could not append audit record after {MAX_WRITE_ATTEMPTS} attempts "
            f"under sequence contention: {last_error}"
        )

    def list_records(
        self, tenant_id: str, *, limit: int = 50, effect: str | None = None
    ) -> list[AuditRecord]:
        """Most recent records first.

        Filtering by outcome uses the outcome-by-time index rather than a table scan, so
        cost stays proportional to results rather than to the log's size.
        """
        if effect:
            response = self.client.query(
                TableName=self._table_name,
                IndexName="outcome-index",
                KeyConditionExpression="gsi1pk = :pk",
                ExpressionAttributeValues={":pk": {"S": f"TENANT#{tenant_id}#OUTCOME#{effect}"}},
                ScanIndexForward=False,
                Limit=limit,
            )
        else:
            response = self.client.query(
                TableName=self._table_name,
                KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
                ExpressionAttributeValues={
                    ":pk": {"S": f"TENANT#{tenant_id}"},
                    ":prefix": {"S": "SEQ#"},
                },
                ScanIndexForward=False,
                Limit=limit,
            )

        return [AuditRecord.from_item(self._deserialize(i)) for i in response.get("Items", [])]

    def verify_chain(self, tenant_id: str, *, limit: int = 1000) -> ChainVerification:
        """Walk the chain oldest-first and check every link."""
        response = self.client.query(
            TableName=self._table_name,
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": f"TENANT#{tenant_id}"},
                ":prefix": {"S": "SEQ#"},
            },
            ScanIndexForward=True,
            Limit=limit,
            ConsistentRead=True,
        )
        records = [AuditRecord.from_item(self._deserialize(i)) for i in response.get("Items", [])]
        verification = verify_records(records)

        if not records:
            return ChainVerification(chain_valid=True, records_checked=0, tenant_id=tenant_id)
        return verification
