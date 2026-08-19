"""Versioned policy bundles and hot reload.

Two properties carry the weight here:

* **Version numbers identify exactly one bundle.** They are written into every audit
  record, so if two publishes could share a number, `bundle_version: 2` would stop
  meaning anything and past decisions would become unreproducible.
* **A policy store outage is not an agent outage.** The provider keeps serving the last
  bundle it successfully read, and says so. Tested rather than asserted in a docstring,
  because it is the behaviour nobody exercises until the incident.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from guardrail_core.operators import PolicyError
from guardrail_core.policy import load_bundle
from guardrail_service.policy_provider import (
    PACKAGED_SOURCE,
    PUBLISHED_SOURCE,
    ActivePolicyProvider,
)
from guardrail_service.storage.policies import (
    ActivePointer,
    InMemoryPolicyRepository,
    PolicyNotFound,
    PublishedBundle,
    content_hash,
    validate_document,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY_FILE = REPO_ROOT / "policies" / "default.yaml"

TENANT = "acme"
BUNDLE = "default"


@pytest.fixture
def document() -> dict[str, Any]:
    return dict(yaml.safe_load(POLICY_FILE.read_text(encoding="utf-8")))


@pytest.fixture
def repository() -> InMemoryPolicyRepository:
    return InMemoryPolicyRepository()


def _minimal(bundle_id: str = "default", version: int = 1, **metadata: Any) -> dict[str, Any]:
    return {
        "apiVersion": "guardrail/v1",
        "metadata": {"bundle_id": bundle_id, "version": version, **metadata},
        "rules": [],
    }


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def test_versions_are_assigned_sequentially(
    repository: InMemoryPolicyRepository, document: dict[str, Any]
) -> None:
    first = repository.publish(TENANT, BUNDLE, document, published_by="alice")
    second = repository.publish(TENANT, BUNDLE, document, published_by="bob")

    assert (first.version, second.version) == (1, 2)


def test_the_store_assigns_the_version_not_the_file(
    repository: InMemoryPolicyRepository,
) -> None:
    """Two authors both typing `version: 2` would otherwise overwrite each other, and
    every audit record's bundle_version would stop identifying a specific policy."""
    published = repository.publish(TENANT, BUNDLE, _minimal(version=99), published_by="alice")

    assert published.version == 1
    assert published.bundle.metadata.version == 1, (
        "the parsed bundle must carry the store's number, not the author's claim"
    )


def test_publishing_is_scoped_per_tenant(repository: InMemoryPolicyRepository) -> None:
    repository.publish("tenant-a", BUNDLE, _minimal(), published_by="a")
    other = repository.publish("tenant-b", BUNDLE, _minimal(), published_by="b")

    assert other.version == 1, "one tenant's history must not advance another's numbering"


def test_an_invalid_bundle_never_reaches_the_store(
    repository: InMemoryPolicyRepository,
) -> None:
    """Activating an unparseable bundle later would break evaluation for every agent at
    once, and surface as an outage rather than as a rejected upload."""
    broken = {
        "apiVersion": "guardrail/v1",
        "metadata": {"bundle_id": "b"},
        "rules": [
            {
                "id": "bad-operator",
                "match": {"tool": "x", "all": [{"path": "args.n", "op": "greater", "value": 1}]},
                "effect": "block",
            }
        ],
    }

    with pytest.raises(PolicyError):
        repository.publish(TENANT, BUNDLE, broken, published_by="alice")

    assert repository.list_versions(TENANT, BUNDLE) == []


def test_a_rule_naming_an_unknown_derived_fact_is_rejected() -> None:
    """The most dangerous policy bug: it looks like coverage and never matches."""
    with pytest.raises(PolicyError, match="unknown derived fact"):
        validate_document(
            {
                "apiVersion": "guardrail/v1",
                "metadata": {"bundle_id": "b"},
                "rules": [
                    {
                        "id": "typo",
                        "match": {
                            "tool": "db.delete_records",
                            "all": [{"path": "derived.recordcount", "op": "gt", "value": 100}],
                        },
                        "effect": "block",
                    }
                ],
            }
        )


def test_content_hash_ignores_key_order(document: dict[str, Any]) -> None:
    """Canonical JSON, so re-serialising on another runtime cannot make an identical
    policy look like a change."""
    reordered = {k: document[k] for k in reversed(list(document))}

    assert content_hash(document) == content_hash(reordered)


def test_content_hash_changes_when_a_threshold_moves(document: dict[str, Any]) -> None:
    edited = copy.deepcopy(document)
    for rule in edited["rules"]:
        if rule["id"] == "db-bulk-delete":
            rule["match"]["all"][0]["value"] = 50

    assert content_hash(edited) != content_hash(document)


def test_semantic_hash_ignores_the_version_number(document: dict[str, Any]) -> None:
    """The store assigns versions, so including the number would mean a bundle could
    never be recognised as identical to the one already live -- and `matches_active`
    would be a field that is always false, which is worse than absent."""
    from guardrail_core.policy import load_bundle
    from guardrail_service.storage.policies import semantic_hash

    v1 = load_bundle({**document, "metadata": {**document["metadata"], "version": 1}})
    v7 = load_bundle({**document, "metadata": {**document["metadata"], "version": 7}})

    assert semantic_hash(v1) == semantic_hash(v7)


def test_semantic_hash_ignores_omitted_defaults(document: dict[str, Any]) -> None:
    """A file that spells out a default and one that omits it express the same policy."""
    from guardrail_core.policy import load_bundle
    from guardrail_service.storage.policies import semantic_hash

    terse = copy.deepcopy(document)
    verbose = copy.deepcopy(document)
    for rule in verbose["rules"]:
        rule.setdefault("enabled", True)
        rule.setdefault("tags", [])

    assert semantic_hash(load_bundle(terse)) == semantic_hash(load_bundle(verbose))


def test_semantic_hash_notices_a_moved_threshold(document: dict[str, Any]) -> None:
    from guardrail_core.policy import load_bundle
    from guardrail_service.storage.policies import semantic_hash

    edited = copy.deepcopy(document)
    for rule in edited["rules"]:
        if rule["id"] == "db-bulk-delete":
            rule["match"]["all"][0]["value"] = 50

    assert semantic_hash(load_bundle(edited)) != semantic_hash(load_bundle(document))


# ---------------------------------------------------------------------------
# Activation and rollback
# ---------------------------------------------------------------------------


def test_activation_requires_a_published_version(
    repository: InMemoryPolicyRepository,
) -> None:
    """Activating a version nobody published would leave agents evaluating against
    nothing."""
    repository.publish(TENANT, BUNDLE, _minimal(), published_by="alice")

    with pytest.raises(PolicyNotFound):
        repository.activate(TENANT, BUNDLE, 7, activated_by="alice")


def test_rollback_is_activation_of_an_earlier_version(
    repository: InMemoryPolicyRepository,
) -> None:
    """Deliberately not a separate code path: a distinct rollback route runs only during
    incidents, which is the worst possible test-coverage profile."""
    repository.publish(TENANT, BUNDLE, _minimal(), published_by="alice")
    repository.publish(TENANT, BUNDLE, _minimal(), published_by="alice")
    repository.activate(TENANT, BUNDLE, 2, activated_by="alice")

    pointer = repository.activate(TENANT, BUNDLE, 1, activated_by="incident-responder")

    assert pointer.active_version == 1
    assert pointer.activated_by == "incident-responder"


def test_rolling_back_does_not_delete_the_newer_version(
    repository: InMemoryPolicyRepository,
) -> None:
    """The history is append-only, so "which policy governed this decision" stays
    answerable. The Lambda role has no DeleteItem, which enforces it beyond convention."""
    repository.publish(TENANT, BUNDLE, _minimal(), published_by="alice")
    repository.publish(TENANT, BUNDLE, _minimal(), published_by="alice")
    repository.activate(TENANT, BUNDLE, 2, activated_by="alice")
    repository.activate(TENANT, BUNDLE, 1, activated_by="alice")

    assert {v.version for v in repository.list_versions(TENANT, BUNDLE)} == {1, 2}
    assert repository.get_version(TENANT, BUNDLE, 2).version == 2


def test_nothing_is_active_until_something_is_activated(
    repository: InMemoryPolicyRepository,
) -> None:
    """Publishing must be safe enough to run in CI on every merge."""
    repository.publish(TENANT, BUNDLE, _minimal(), published_by="alice")

    assert repository.get_active(TENANT, BUNDLE) is None


# ---------------------------------------------------------------------------
# Item round-tripping
# ---------------------------------------------------------------------------


def test_a_published_bundle_round_trips_through_its_item(document: dict[str, Any]) -> None:
    """Thresholds include floats, which DynamoDB cannot store natively -- hence the
    canonical-JSON string. A lossy round trip here would silently shift a threshold."""
    published = PublishedBundle(
        tenant_id=TENANT,
        bundle_id=BUNDLE,
        version=4,
        document=document,
        content_hash=content_hash(document),
        published_at="2026-08-19T00:00:00.000+00:00",
        published_by="alice",
        description="threshold tuning",
    )

    restored = PublishedBundle.from_item(published.to_item())

    assert restored.document == document
    assert restored.version == 4
    assert restored.tenant_id == TENANT
    assert restored.bundle_id == BUNDLE
    assert restored.content_hash == published.content_hash


def test_a_float_threshold_survives_the_round_trip() -> None:
    document = {
        "apiVersion": "guardrail/v1",
        "metadata": {"bundle_id": "b"},
        "rules": [
            {
                "id": "refund-cap",
                "match": {
                    "tool": "payments.refund",
                    "all": [{"path": "args.amount", "op": "gt", "value": 1000.5}],
                },
                "effect": "block",
            }
        ],
    }
    published = PublishedBundle(
        tenant_id=TENANT,
        bundle_id=BUNDLE,
        version=1,
        document=document,
        content_hash=content_hash(document),
        published_at="t",
        published_by="alice",
    )

    restored = PublishedBundle.from_item(published.to_item())

    assert restored.document["rules"][0]["match"]["all"][0]["value"] == 1000.5


def test_the_active_pointer_round_trips() -> None:
    pointer = ActivePointer(
        tenant_id=TENANT,
        bundle_id=BUNDLE,
        active_version=3,
        activated_at="2026-08-19T00:00:00.000+00:00",
        activated_by="key-1:alice",
    )

    restored = ActivePointer.from_item(pointer.to_item())

    assert restored == pointer


def test_policy_items_do_not_collide_with_audit_items() -> None:
    """Policies share the audit table to stay inside 25 WCU. They must land in their own
    partition, or a policy write could interleave with the hash chain's sequence."""
    item = ActivePointer(TENANT, BUNDLE, 1, "t", "alice").to_item()

    assert item["pk"] == f"TENANT#{TENANT}#POLICY#{BUNDLE}"
    assert item["pk"] != f"TENANT#{TENANT}", "must not share the audit chain's partition"


# ---------------------------------------------------------------------------
# Hot reload
# ---------------------------------------------------------------------------


class _Clock:
    """A controllable monotonic clock, so refresh windows are tested in microseconds."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _CountingRepository(InMemoryPolicyRepository):
    """Counts reads, so "does the cache actually cache?" is measurable."""

    def __init__(self) -> None:
        super().__init__()
        self.active_reads = 0
        self.version_reads = 0

    def get_active(self, tenant_id: str, bundle_id: str) -> ActivePointer | None:
        self.active_reads += 1
        return super().get_active(tenant_id, bundle_id)

    def get_version(self, tenant_id: str, bundle_id: str, version: int) -> PublishedBundle:
        self.version_reads += 1
        return super().get_version(tenant_id, bundle_id, version)


@pytest.fixture
def packaged() -> Any:
    return load_bundle(_minimal(bundle_id="packaged", version=1))


def test_the_packaged_bundle_serves_until_something_is_activated(
    packaged: Any, repository: InMemoryPolicyRepository
) -> None:
    """A fresh deployment must govern correctly before anyone touches the policy API."""
    provider = ActivePolicyProvider(repository, packaged)

    state = provider.state(TENANT)

    assert state.source == PACKAGED_SOURCE
    assert state.degraded is False


def test_an_activation_is_picked_up_after_the_refresh_window(packaged: Any) -> None:
    clock = _Clock()
    repository = _CountingRepository()
    provider = ActivePolicyProvider(repository, packaged, refresh_seconds=30.0, clock=clock)

    assert provider.state(TENANT).source == PACKAGED_SOURCE

    repository.publish(TENANT, BUNDLE, _minimal(bundle_id="default"), published_by="alice")
    repository.activate(TENANT, BUNDLE, 1, activated_by="alice")

    # Still inside the window: the old answer stands, which is the bounded staleness
    # the setting exists to make explicit.
    assert provider.state(TENANT).source == PACKAGED_SOURCE

    clock.advance(31)

    state = provider.state(TENANT)
    assert state.source == PUBLISHED_SOURCE
    assert state.version == 1


def test_the_cache_is_not_re_read_inside_the_refresh_window(packaged: Any) -> None:
    clock = _Clock()
    repository = _CountingRepository()
    provider = ActivePolicyProvider(repository, packaged, refresh_seconds=30.0, clock=clock)

    for _ in range(50):
        provider.get(TENANT)

    assert repository.active_reads == 1, (
        "a read per request would turn every policy decision into a database call"
    )


def test_an_unchanged_version_is_not_refetched(packaged: Any) -> None:
    """The pointer is re-read on the timer; the bundle body is fetched only when the
    version actually moved."""
    clock = _Clock()
    repository = _CountingRepository()
    repository.publish(TENANT, BUNDLE, _minimal(), published_by="alice")
    repository.activate(TENANT, BUNDLE, 1, activated_by="alice")

    # Measured from after setup: `activate` performs its own existence check, which
    # legitimately reads a version and would otherwise be counted against the provider.
    baseline = repository.version_reads

    provider = ActivePolicyProvider(repository, packaged, refresh_seconds=30.0, clock=clock)
    provider.get(TENANT)

    for _ in range(5):
        clock.advance(31)
        provider.get(TENANT)

    assert repository.active_reads == 6, "the pointer is re-read once per refresh window"
    assert repository.version_reads - baseline == 1, (
        "the bundle body must be fetched only when the version actually changed"
    )


def test_invalidate_makes_an_activation_visible_immediately(packaged: Any) -> None:
    """So the operator who clicked activate sees the effect on their next request rather
    than up to refresh_seconds later."""
    clock = _Clock()
    repository = InMemoryPolicyRepository()
    provider = ActivePolicyProvider(repository, packaged, refresh_seconds=300.0, clock=clock)

    provider.get(TENANT)
    repository.publish(TENANT, BUNDLE, _minimal(), published_by="alice")
    repository.activate(TENANT, BUNDLE, 1, activated_by="alice")
    provider.invalidate(TENANT)

    assert provider.state(TENANT).source == PUBLISHED_SOURCE


def test_a_store_outage_keeps_serving_the_last_known_good_bundle(packaged: Any) -> None:
    """**The behaviour nobody exercises until the incident.** A failed read does not make
    the previously active policy wrong, and halting an agent fleet over it would be a
    self-inflicted outage."""
    clock = _Clock()
    repository = _CountingRepository()
    repository.publish(TENANT, BUNDLE, _minimal(bundle_id="published"), published_by="alice")
    repository.activate(TENANT, BUNDLE, 1, activated_by="alice")

    provider = ActivePolicyProvider(repository, packaged, refresh_seconds=30.0, clock=clock)
    assert provider.state(TENANT).source == PUBLISHED_SOURCE

    def explode(tenant_id: str, bundle_id: str) -> ActivePointer | None:
        raise RuntimeError("DynamoDB unreachable")

    repository.get_active = explode  # type: ignore[method-assign]
    clock.advance(31)

    state = provider.state(TENANT)

    assert state.source == PUBLISHED_SOURCE
    assert state.version == 1
    assert state.degraded is True
    assert state.error is not None and "unreachable" in state.error


def test_a_cold_start_with_a_broken_store_falls_back_to_the_packaged_bundle(
    packaged: Any,
) -> None:
    """The one genuinely ambiguous case. Failing every request would halt the fleet;
    allowing everything is the failure this product exists to prevent. Serving the
    reviewed bundle that shipped with the build is the third option -- and it is marked
    degraded so it is never mistaken for a confirmed answer."""

    class BrokenRepository(InMemoryPolicyRepository):
        def get_active(self, tenant_id: str, bundle_id: str) -> ActivePointer | None:
            raise RuntimeError("DynamoDB unreachable")

    provider = ActivePolicyProvider(BrokenRepository(), packaged)

    state = provider.state(TENANT)

    assert state.source == PACKAGED_SOURCE
    assert state.degraded is True
    assert state.bundle is packaged


def test_a_degraded_provider_recovers_when_the_store_returns(packaged: Any) -> None:
    clock = _Clock()
    repository = _CountingRepository()
    repository.publish(TENANT, BUNDLE, _minimal(), published_by="alice")
    repository.activate(TENANT, BUNDLE, 1, activated_by="alice")

    working = repository.get_active

    def explode(tenant_id: str, bundle_id: str) -> ActivePointer | None:
        raise RuntimeError("DynamoDB unreachable")

    repository.get_active = explode  # type: ignore[method-assign]
    provider = ActivePolicyProvider(repository, packaged, refresh_seconds=30.0, clock=clock)
    assert provider.state(TENANT).degraded is True

    repository.get_active = working  # type: ignore[method-assign]
    clock.advance(31)

    state = provider.state(TENANT)
    assert state.degraded is False
    assert state.source == PUBLISHED_SOURCE


def test_each_tenant_gets_its_own_active_policy(packaged: Any) -> None:
    """Policy is already keyed by tenant, so M5's multi-tenancy does not have to
    retrofit the reload path."""
    repository = InMemoryPolicyRepository()
    repository.publish("tenant-a", BUNDLE, _minimal(bundle_id="for-a"), published_by="a")
    repository.activate("tenant-a", BUNDLE, 1, activated_by="a")

    provider = ActivePolicyProvider(repository, packaged)

    assert provider.state("tenant-a").source == PUBLISHED_SOURCE
    assert provider.state("tenant-b").source == PACKAGED_SOURCE


def test_a_shadow_bundle_is_reported_as_shadow(packaged: Any) -> None:
    repository = InMemoryPolicyRepository()
    repository.publish(TENANT, BUNDLE, _minimal(mode="shadow"), published_by="alice")
    repository.activate(TENANT, BUNDLE, 1, activated_by="alice")

    provider = ActivePolicyProvider(repository, packaged)

    assert provider.get(TENANT).is_shadow is True
