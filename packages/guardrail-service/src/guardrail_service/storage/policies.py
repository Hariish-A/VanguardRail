"""Versioned policy bundles: publish, activate, roll back, and hot reload.

Until now the running policy was whatever was baked into the deployment. That is a real
limitation for a governance product: tightening a rule after an incident should take
seconds, not a redeploy, and proving *which* policy was in force last Tuesday should not
require reading a git log against a deploy history.

## The shape

Every published bundle is an immutable, numbered version. A separate pointer names the
one that is active. Publishing does not change behaviour; activating does. Rolling back
is just activating an older version -- there is no distinct rollback path to get wrong,
and no way for a rollback to invent a bundle nobody reviewed.

    pk = TENANT#<tenant>#POLICY#<bundle_id>
      sk = VERSION#000000000003   immutable published bundle
      sk = ACTIVE                 pointer: {"active_version": 3}

## Why nothing is ever deleted

The Lambda's IAM role has `PutItem`, `GetItem`, `Query`, and `UpdateItem` -- deliberately
**not** `DeleteItem`. So the version history is append-only by permission, not by
convention. "Which policy governed this decision, and who activated it?" is answerable
from the table itself, and cannot be quietly rewritten by the service that answers it.

## Why this shares the audit table

A separate table needs its own provisioned capacity, and 15 of the account's free 25 WCU
are already committed by the audit table and its two indexes. Policy items live in their
own partitions of the same table, add no index, and therefore cost nothing extra.

## Storage format

The bundle is stored as its **canonical JSON string**, exactly like an audit payload and
for the same reason: DynamoDB cannot store Python floats, and a policy threshold of
`1000.50` is entirely ordinary. Converting to Decimal and back risks a value that no
longer round-trips, which for a policy means a threshold that silently shifts.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from guardrail_core.operators import PolicyError
from guardrail_core.policy import PolicyBundle, load_bundle

from guardrail_service.storage.audit import canonical_json

MAX_PUBLISH_ATTEMPTS = 5
"""Bounded retries when two publishes race for the same version number."""

ACTIVE_SK = "ACTIVE"
VERSION_PREFIX = "VERSION#"


class PolicyNotFound(LookupError):
    """No such bundle or version for this tenant."""


class PolicyStoreError(RuntimeError):
    """The policy store could not complete the operation."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def content_hash(document: dict[str, Any]) -> str:
    """Fingerprint of a bundle's content, independent of its version number.

    Lets the API answer "is this the same policy under a new number?" -- which is the
    difference between a real change and someone re-uploading the same file, and it is
    worth knowing before an activation.
    """
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def semantic_hash(bundle: PolicyBundle) -> str:
    """Fingerprint of what a bundle *means*, ignoring how it was written.

    Distinct from `content_hash`, which fingerprints the submitted document byte for
    byte. Two files can differ in key order, omitted defaults, or version number and
    still express exactly the same policy -- so comparing raw documents would report
    "different" almost always, which makes the comparison worthless.

    `metadata.version` is excluded on purpose: the store assigns it, so including it
    would mean a bundle could never be recognised as identical to the one already live.
    """
    normalised = bundle.model_dump(mode="json")
    metadata = dict(normalised.get("metadata", {}))
    metadata.pop("version", None)
    normalised["metadata"] = metadata
    return hashlib.sha256(canonical_json(normalised).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PublishedBundle:
    """One immutable published version."""

    tenant_id: str
    bundle_id: str
    version: int
    document: dict[str, Any]
    content_hash: str
    published_at: str
    published_by: str
    description: str = ""

    @property
    def bundle(self) -> PolicyBundle:
        """The parsed bundle, with its stored version number authoritative.

        The number the store assigned wins over whatever `metadata.version` the author
        typed. Otherwise two people could publish files both claiming v2, and every
        audit record's `bundle_version` would stop identifying anything.
        """
        document = {
            **self.document,
            "metadata": {**self.document.get("metadata", {}), "version": self.version},
        }
        return load_bundle(document)

    def to_item(self) -> dict[str, Any]:
        return {
            "pk": f"TENANT#{self.tenant_id}#POLICY#{self.bundle_id}",
            "sk": f"{VERSION_PREFIX}{self.version:012d}",
            "version": self.version,
            "bundle_id": self.bundle_id,
            "bundle_json": canonical_json(self.document),
            "content_hash": self.content_hash,
            "published_at": self.published_at,
            "published_by": self.published_by,
            "description": self.description,
        }

    @staticmethod
    def from_item(item: dict[str, Any]) -> PublishedBundle:
        pk = str(item["pk"])
        tenant_id, _, bundle_id = pk.removeprefix("TENANT#").partition("#POLICY#")
        return PublishedBundle(
            tenant_id=tenant_id,
            bundle_id=bundle_id,
            version=int(item["version"]),
            document=json.loads(item["bundle_json"]),
            content_hash=str(item.get("content_hash", "")),
            published_at=str(item.get("published_at", "")),
            published_by=str(item.get("published_by", "unknown")),
            description=str(item.get("description", "")),
        )


@dataclass(frozen=True)
class ActivePointer:
    """Which version is currently in force."""

    tenant_id: str
    bundle_id: str
    active_version: int
    activated_at: str
    activated_by: str

    def to_item(self) -> dict[str, Any]:
        return {
            "pk": f"TENANT#{self.tenant_id}#POLICY#{self.bundle_id}",
            "sk": ACTIVE_SK,
            "active_version": self.active_version,
            "activated_at": self.activated_at,
            "activated_by": self.activated_by,
        }

    @staticmethod
    def from_item(item: dict[str, Any]) -> ActivePointer:
        pk = str(item["pk"])
        tenant_id, _, bundle_id = pk.removeprefix("TENANT#").partition("#POLICY#")
        return ActivePointer(
            tenant_id=tenant_id,
            bundle_id=bundle_id,
            active_version=int(item["active_version"]),
            activated_at=str(item.get("activated_at", "")),
            activated_by=str(item.get("activated_by", "unknown")),
        )


def validate_document(document: Any) -> dict[str, Any]:
    """Reject a bad bundle at publish time, which is the only safe moment.

    A policy that fails to parse must never reach the store. If it did, activating it
    would break evaluation for every agent at once, and the failure would surface as an
    outage rather than as a rejected upload with a line number.
    """
    if not isinstance(document, dict):
        raise PolicyError(f"policy bundle must be a mapping, got {type(document).__name__}")

    load_bundle(document)  # raises PolicyError with full detail
    return document


class PolicyRepository(Protocol):
    """Storage interface, so the API and the hot-reload cache can be tested without AWS."""

    def publish(
        self,
        tenant_id: str,
        bundle_id: str,
        document: dict[str, Any],
        *,
        published_by: str,
        description: str = "",
    ) -> PublishedBundle: ...

    def get_version(self, tenant_id: str, bundle_id: str, version: int) -> PublishedBundle: ...

    def list_versions(
        self, tenant_id: str, bundle_id: str, *, limit: int = 50
    ) -> list[PublishedBundle]: ...

    def get_active(self, tenant_id: str, bundle_id: str) -> ActivePointer | None: ...

    def activate(
        self, tenant_id: str, bundle_id: str, version: int, *, activated_by: str
    ) -> ActivePointer: ...


# ---------------------------------------------------------------------------
# In-memory
# ---------------------------------------------------------------------------


class InMemoryPolicyRepository:
    """Reference implementation for tests and local development.

    Shares the version-assignment and validation semantics with the DynamoDB
    implementation, so a test passing here exercises the invariants that run deployed.
    """

    def __init__(self) -> None:
        self._versions: dict[tuple[str, str], list[PublishedBundle]] = {}
        self._active: dict[tuple[str, str], ActivePointer] = {}

    def publish(
        self,
        tenant_id: str,
        bundle_id: str,
        document: dict[str, Any],
        *,
        published_by: str,
        description: str = "",
    ) -> PublishedBundle:
        validate_document(document)
        chain = self._versions.setdefault((tenant_id, bundle_id), [])
        published = PublishedBundle(
            tenant_id=tenant_id,
            bundle_id=bundle_id,
            version=len(chain) + 1,
            document=document,
            content_hash=content_hash(document),
            published_at=_now_iso(),
            published_by=published_by,
            description=description,
        )
        chain.append(published)
        return published

    def get_version(self, tenant_id: str, bundle_id: str, version: int) -> PublishedBundle:
        for published in self._versions.get((tenant_id, bundle_id), []):
            if published.version == version:
                return published
        raise PolicyNotFound(f"no version {version} of bundle {bundle_id!r}")

    def list_versions(
        self, tenant_id: str, bundle_id: str, *, limit: int = 50
    ) -> list[PublishedBundle]:
        chain = self._versions.get((tenant_id, bundle_id), [])
        return list(reversed(chain))[:limit]

    def get_active(self, tenant_id: str, bundle_id: str) -> ActivePointer | None:
        return self._active.get((tenant_id, bundle_id))

    def activate(
        self, tenant_id: str, bundle_id: str, version: int, *, activated_by: str
    ) -> ActivePointer:
        self.get_version(tenant_id, bundle_id, version)  # raises if it does not exist
        pointer = ActivePointer(
            tenant_id=tenant_id,
            bundle_id=bundle_id,
            active_version=version,
            activated_at=_now_iso(),
            activated_by=activated_by,
        )
        self._active[(tenant_id, bundle_id)] = pointer
        return pointer


# ---------------------------------------------------------------------------
# DynamoDB
# ---------------------------------------------------------------------------


class DynamoDBPolicyRepository:
    """DynamoDB-backed policy store, sharing the audit table."""

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

    def _pk(self, tenant_id: str, bundle_id: str) -> str:
        return f"TENANT#{tenant_id}#POLICY#{bundle_id}"

    def _latest_version(self, tenant_id: str, bundle_id: str) -> int:
        """Highest published version, read strongly consistently.

        Consistency matters for the same reason it does in the audit chain: an
        eventually consistent read could miss a version a concurrent publisher just
        wrote, and the two would collide on the next number.
        """
        response = self.client.query(
            TableName=self._table_name,
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": self._pk(tenant_id, bundle_id)},
                ":prefix": {"S": VERSION_PREFIX},
            },
            ScanIndexForward=False,
            Limit=1,
            ConsistentRead=True,
        )
        items = response.get("Items", [])
        return int(self._deserialize(items[0])["version"]) if items else 0

    def publish(
        self,
        tenant_id: str,
        bundle_id: str,
        document: dict[str, Any],
        *,
        published_by: str,
        description: str = "",
    ) -> PublishedBundle:
        """Store a new immutable version.

        The version number is assigned by the store, never taken from the file. Two
        authors both typing `version: 2` would otherwise overwrite each other, and every
        audit record's `bundle_version` would stop identifying a specific policy.

        The conditional put is what makes that safe under concurrency: whoever claims a
        number keeps it, and the loser re-reads and takes the next one.
        """
        from botocore.exceptions import ClientError

        validate_document(document)
        last_error: Exception | None = None

        for attempt in range(MAX_PUBLISH_ATTEMPTS):
            published = PublishedBundle(
                tenant_id=tenant_id,
                bundle_id=bundle_id,
                version=self._latest_version(tenant_id, bundle_id) + 1,
                document=document,
                content_hash=content_hash(document),
                published_at=_now_iso(),
                published_by=published_by,
                description=description,
            )

            try:
                self.client.put_item(
                    TableName=self._table_name,
                    Item=self._serialize(published.to_item()),
                    ConditionExpression="attribute_not_exists(sk)",
                )
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
                last_error = exc
                time.sleep(0.02 * (2**attempt))
                continue
            else:
                return published

        raise PolicyStoreError(
            f"could not publish a new version of {bundle_id!r} after "
            f"{MAX_PUBLISH_ATTEMPTS} attempts under contention: {last_error}"
        )

    def get_version(self, tenant_id: str, bundle_id: str, version: int) -> PublishedBundle:
        response = self.client.get_item(
            TableName=self._table_name,
            Key={
                "pk": {"S": self._pk(tenant_id, bundle_id)},
                "sk": {"S": f"{VERSION_PREFIX}{version:012d}"},
            },
        )
        item = response.get("Item")
        if not item:
            raise PolicyNotFound(f"no version {version} of bundle {bundle_id!r}")
        return PublishedBundle.from_item(self._deserialize(item))

    def list_versions(
        self, tenant_id: str, bundle_id: str, *, limit: int = 50
    ) -> list[PublishedBundle]:
        """Newest first."""
        response = self.client.query(
            TableName=self._table_name,
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": self._pk(tenant_id, bundle_id)},
                ":prefix": {"S": VERSION_PREFIX},
            },
            ScanIndexForward=False,
            Limit=limit,
        )
        return [PublishedBundle.from_item(self._deserialize(i)) for i in response.get("Items", [])]

    def get_active(self, tenant_id: str, bundle_id: str) -> ActivePointer | None:
        """Read the active-version pointer.

        Deliberately **not** a consistent read. This runs on the hot path via the reload
        cache, and an eventually consistent GetItem costs half the read capacity. The
        cost of being a second behind on a policy activation is one second of the old
        policy; the cost of doubling RCU on every refresh is throttling the free tier.
        """
        response = self.client.get_item(
            TableName=self._table_name,
            Key={"pk": {"S": self._pk(tenant_id, bundle_id)}, "sk": {"S": ACTIVE_SK}},
        )
        item = response.get("Item")
        return ActivePointer.from_item(self._deserialize(item)) if item else None

    def activate(
        self, tenant_id: str, bundle_id: str, version: int, *, activated_by: str
    ) -> ActivePointer:
        """Point the active marker at an existing version.

        Rollback uses this same call with an older number -- there is no separate
        rollback path, so there is no separate rollback bug.

        The existence check is what makes it safe: activating a version that was never
        published would leave every agent evaluating against nothing. Last write wins
        between two simultaneous activations, which is acceptable because the loser's
        intent is recorded either way -- `activated_by` and `activated_at` are stored,
        and the version history is immutable.
        """
        self.get_version(tenant_id, bundle_id, version)

        pointer = ActivePointer(
            tenant_id=tenant_id,
            bundle_id=bundle_id,
            active_version=version,
            activated_at=_now_iso(),
            activated_by=activated_by,
        )
        self.client.put_item(
            TableName=self._table_name,
            Item=self._serialize(pointer.to_item()),
        )
        return pointer
