"""Pending human-review decisions.

When policy returns `require_hitl`, the action is suspended and a decision record is
created. A reviewer approves or denies it; the agent resumes or reports the refusal.

## Three properties this has to get right

**Exactly one winner.** Two reviewers can open the queue and click at the same moment.
Resolution is a conditional update on `status = PENDING`, so precisely one succeeds and
the other receives a clean 409 rather than silently overwriting a colleague's judgement.

**Silence is never consent.** An approval request nobody answers must not become an
approval. Every decision carries an expiry and an `on_timeout` action, defaulting to
deny.

**Expiry is computed, never delegated.** DynamoDB's TTL deletes items on a best-effort
schedule that can lag by 48 hours, so it is used only to reclaim storage. Whether a
decision has expired is decided by comparing timestamps at read time. Trusting TTL for
correctness would leave a request that expired an hour ago still answerable as pending.

## Why this shares the audit table

A second table would need its own provisioned capacity, and the account's entire free
allowance is 25 WCU / 25 RCU -- already 15/15 after the audit table and its two indexes.
Pending decisions instead live in the same table under a `DECISION#` sort key and reuse
the existing outcome index as a **sparse index**: the index attributes are written only
while a decision is pending, and removed on resolution, so it drops out of the queue
query automatically and costs no extra capacity.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

DecisionStatus = Literal["pending", "approved", "denied", "expired"]

DEFAULT_TIMEOUT_SECONDS = 900

# Kept well beyond the decision's own expiry so an expired-but-unreclaimed record can
# still be read and reported as expired rather than vanishing into a 404.
RECORD_RETENTION_SECONDS = 7 * 24 * 60 * 60


class DecisionNotFound(LookupError):
    """No such decision for this tenant."""


class DecisionAlreadyResolved(RuntimeError):
    """Someone else resolved it first. Surfaces to the caller as 409."""

    def __init__(self, decision_id: str, status: str, reviewer: str | None) -> None:
        self.decision_id = decision_id
        self.status = status
        self.reviewer = reviewer
        by = f" by {reviewer}" if reviewer else ""
        super().__init__(f"Decision {decision_id} was already {status}{by}.")


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds")


@dataclass
class PendingDecision:
    """One action awaiting a human.

    Carries a full snapshot of the action rather than a reference to it. A reviewer
    deciding on "email.send" needs to see the recipients, the subject, and which rule
    fired -- and needs to see exactly what was true when the decision was raised, not
    what the policy says now.
    """

    decision_id: str
    tenant_id: str
    status: DecisionStatus
    tool: str
    arguments: dict[str, Any]
    agent_id: str
    session_id: str
    matched_rules: list[dict[str, Any]]
    message: str | None
    created_at: str
    expires_at: int
    on_timeout: Literal["deny", "allow"]
    reviewers: list[str]
    audit_seq: int
    resolved_at: str | None = None
    reviewer: str | None = None
    reason: str | None = None

    @property
    def is_expired(self) -> bool:
        """Whether the review window has closed, computed rather than trusted to TTL."""
        return self.status == "pending" and time.time() >= self.expires_at

    @property
    def effective_status(self) -> DecisionStatus:
        """What the decision means right now.

        A pending decision past its expiry resolves to `expired`, and `on_timeout`
        determines whether that permits the action. This is computed on every read so a
        lagging TTL cannot leave a stale request answerable.
        """
        return "expired" if self.is_expired else self.status

    @property
    def allows_execution(self) -> bool:
        """Whether the agent may now proceed."""
        status = self.effective_status
        if status == "approved":
            return True
        if status == "expired":
            return self.on_timeout == "allow"
        return False

    @property
    def seconds_remaining(self) -> int:
        return max(0, int(self.expires_at - time.time()))

    def to_item(self) -> dict[str, Any]:
        """DynamoDB item.

        `gsi1pk`/`gsi1sk` are present only while pending. Removing them on resolution
        makes the review queue a sparse index -- resolved decisions leave it without a
        scan, a filter, or a second table.
        """
        item: dict[str, Any] = {
            "pk": f"TENANT#{self.tenant_id}",
            "sk": f"DECISION#{self.decision_id}",
            "decision_id": self.decision_id,
            "status": self.status,
            "tool": self.tool,
            # Stored as canonical JSON for the same reason audit payloads are: DynamoDB
            # cannot hold Python floats, and tool arguments legitimately contain them.
            "arguments_json": json.dumps(self.arguments, separators=(",", ":"), sort_keys=True),
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "matched_rules_json": json.dumps(self.matched_rules, separators=(",", ":")),
            "message": self.message or "",
            "created_at": self.created_at,
            "decision_expires_at": self.expires_at,
            "on_timeout": self.on_timeout,
            "reviewers": self.reviewers or ["*"],
            "audit_seq": self.audit_seq,
            # TTL attribute. Deliberately far later than the review window, so an expired
            # decision remains readable and reportable instead of 404-ing.
            "expires_at": int(time.time()) + RECORD_RETENTION_SECONDS,
        }

        if self.status == "pending":
            item["gsi1pk"] = f"TENANT#{self.tenant_id}#PENDING"
            item["gsi1sk"] = f"{self.created_at}#{self.decision_id}"

        if self.resolved_at:
            item["resolved_at"] = self.resolved_at
        if self.reviewer:
            item["reviewer"] = self.reviewer
        if self.reason:
            item["reason"] = self.reason

        return item

    @staticmethod
    def from_item(item: dict[str, Any]) -> PendingDecision:
        return PendingDecision(
            decision_id=str(item["decision_id"]),
            tenant_id=str(item["pk"]).removeprefix("TENANT#"),
            status=str(item["status"]),  # type: ignore[arg-type]
            tool=str(item["tool"]),
            arguments=json.loads(item.get("arguments_json") or "{}"),
            agent_id=str(item.get("agent_id", "")),
            session_id=str(item.get("session_id", "")),
            matched_rules=json.loads(item.get("matched_rules_json") or "[]"),
            message=str(item.get("message") or "") or None,
            created_at=str(item["created_at"]),
            expires_at=int(item["decision_expires_at"]),
            on_timeout=str(item.get("on_timeout", "deny")),  # type: ignore[arg-type]
            reviewers=list(item.get("reviewers") or []),
            audit_seq=int(item.get("audit_seq", 0)),
            resolved_at=str(item["resolved_at"]) if item.get("resolved_at") else None,
            reviewer=str(item["reviewer"]) if item.get("reviewer") else None,
            reason=str(item["reason"]) if item.get("reason") else None,
        )


def build_pending(
    *,
    decision_id: str,
    tenant_id: str,
    tool: str,
    arguments: dict[str, Any],
    agent_id: str,
    session_id: str,
    matched_rules: list[dict[str, Any]],
    message: str | None,
    audit_seq: int,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    on_timeout: Literal["deny", "allow"] = "deny",
    reviewers: list[str] | None = None,
) -> PendingDecision:
    """Create a decision awaiting review."""
    now = _now()
    return PendingDecision(
        decision_id=decision_id,
        tenant_id=tenant_id,
        status="pending",
        tool=tool,
        arguments=arguments,
        agent_id=agent_id,
        session_id=session_id,
        matched_rules=matched_rules,
        message=message,
        created_at=_iso(now),
        expires_at=int(now.timestamp()) + timeout_seconds,
        on_timeout=on_timeout,
        reviewers=reviewers or ["*"],
        audit_seq=audit_seq,
    )


class DecisionRepository(Protocol):
    """Storage interface, so the API can be tested without AWS."""

    def create(self, decision: PendingDecision) -> None: ...

    def get(self, tenant_id: str, decision_id: str) -> PendingDecision: ...

    def list_pending(self, tenant_id: str, *, limit: int = 50) -> list[PendingDecision]: ...

    def resolve(
        self,
        tenant_id: str,
        decision_id: str,
        *,
        approve: bool,
        reviewer: str,
        reason: str,
    ) -> PendingDecision: ...


class InMemoryDecisionRepository:
    """Reference implementation for tests and local development."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], PendingDecision] = {}

    def create(self, decision: PendingDecision) -> None:
        self._items[(decision.tenant_id, decision.decision_id)] = decision

    def get(self, tenant_id: str, decision_id: str) -> PendingDecision:
        try:
            return self._items[(tenant_id, decision_id)]
        except KeyError:
            raise DecisionNotFound(decision_id) from None

    def list_pending(self, tenant_id: str, *, limit: int = 50) -> list[PendingDecision]:
        pending = [
            d
            for (t, _), d in self._items.items()
            if t == tenant_id and d.status == "pending" and not d.is_expired
        ]
        return sorted(pending, key=lambda d: d.created_at)[:limit]

    def resolve(
        self,
        tenant_id: str,
        decision_id: str,
        *,
        approve: bool,
        reviewer: str,
        reason: str,
    ) -> PendingDecision:
        current = self.get(tenant_id, decision_id)

        if current.status != "pending":
            raise DecisionAlreadyResolved(decision_id, current.status, current.reviewer)
        if current.is_expired:
            raise DecisionAlreadyResolved(decision_id, "expired", None)

        resolved = PendingDecision(
            **{
                **current.__dict__,
                "status": "approved" if approve else "denied",
                "resolved_at": _iso(_now()),
                "reviewer": reviewer,
                "reason": reason,
            }
        )
        self._items[(tenant_id, decision_id)] = resolved
        return resolved


class DynamoDBDecisionRepository:
    """DynamoDB-backed decision store, sharing the audit table."""

    def __init__(self, table_name: str, *, client: Any = None) -> None:
        self._table_name = table_name
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("dynamodb")
        return self._client

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

    def create(self, decision: PendingDecision) -> None:
        self.client.put_item(
            TableName=self._table_name,
            Item=self._serialize(decision.to_item()),
        )

    def get(self, tenant_id: str, decision_id: str) -> PendingDecision:
        response = self.client.get_item(
            TableName=self._table_name,
            Key={
                "pk": {"S": f"TENANT#{tenant_id}"},
                "sk": {"S": f"DECISION#{decision_id}"},
            },
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            raise DecisionNotFound(decision_id)
        return PendingDecision.from_item(self._deserialize(item))

    def list_pending(self, tenant_id: str, *, limit: int = 50) -> list[PendingDecision]:
        """The review queue, via the sparse index.

        Expired-but-unresolved decisions are filtered out here rather than in the query:
        they are no longer actionable, and showing a reviewer a request they cannot
        answer wastes their attention.
        """
        response = self.client.query(
            TableName=self._table_name,
            IndexName="outcome-index",
            KeyConditionExpression="gsi1pk = :pk",
            ExpressionAttributeValues={":pk": {"S": f"TENANT#{tenant_id}#PENDING"}},
            ScanIndexForward=True,
            Limit=limit,
        )
        decisions = [
            PendingDecision.from_item(self._deserialize(i)) for i in response.get("Items", [])
        ]
        return [d for d in decisions if not d.is_expired]

    def resolve(
        self,
        tenant_id: str,
        decision_id: str,
        *,
        approve: bool,
        reviewer: str,
        reason: str,
    ) -> PendingDecision:
        """Approve or deny, atomically.

        The condition is what makes concurrent review safe: only a record still in
        `pending` can be moved, so simultaneous clicks produce one winner and one 409
        rather than one judgement silently overwriting another.

        `REMOVE gsi1pk, gsi1sk` drops the record out of the sparse queue index in the
        same atomic write, so a resolved decision cannot linger in anyone's queue.
        """
        from botocore.exceptions import ClientError

        # Read first so expiry can be evaluated, and so a 409 can name who won.
        current = self.get(tenant_id, decision_id)
        if current.is_expired:
            raise DecisionAlreadyResolved(decision_id, "expired", None)

        now = int(time.time())
        try:
            response = self.client.update_item(
                TableName=self._table_name,
                Key={
                    "pk": {"S": f"TENANT#{tenant_id}"},
                    "sk": {"S": f"DECISION#{decision_id}"},
                },
                UpdateExpression=(
                    "SET #status = :new_status, resolved_at = :resolved_at, "
                    "reviewer = :reviewer, reason = :reason "
                    "REMOVE gsi1pk, gsi1sk"
                ),
                ConditionExpression=("#status = :pending AND decision_expires_at > :now"),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":new_status": {"S": "approved" if approve else "denied"},
                    ":pending": {"S": "pending"},
                    ":resolved_at": {"S": _iso(_now())},
                    ":reviewer": {"S": reviewer},
                    ":reason": {"S": reason},
                    ":now": {"N": str(now)},
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            # Re-read to report *why* it failed: someone else won, or it expired between
            # the read above and this write.
            latest = self.get(tenant_id, decision_id)
            status = "expired" if latest.is_expired else latest.status
            raise DecisionAlreadyResolved(decision_id, status, latest.reviewer) from exc

        return PendingDecision.from_item(self._deserialize(response["Attributes"]))
