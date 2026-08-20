"""The review queue, against the DynamoDB repository.

**The regression test here is
`test_a_live_decision_is_found_behind_a_page_of_expired_ones`.**

It reproduces a defect found in production. The queue had been silently returning nothing:
the agent held an action, the console said the queue was empty, and every test passed.

The cause was ordering, in two senses. `list_pending` read one page of `Limit=50` from an
index sorted **oldest-first**, then filtered expired decisions out in Python. Expired
entries never leave that index -- only resolution removes the index keys, and expiry is
not a write -- so they accumulate until the record's TTL reclaims them a week later. Once
more than 50 had piled up, the first page was entirely stale, every item was filtered
away, and live decisions sitting beyond it were never read.

Observed on the live table: **111 index entries, 3 of them live, queue returning 0.**

Why no test caught it: `InMemoryDecisionRepository.list_pending` filters *then* limits,
which is the correct order. Every existing test ran against that one. Two implementations
of a single contract agreed on the signature and disagreed on the semantics, and the one
under test was the one that was right.

So these tests drive the **DynamoDB** implementation through a stub client that models the
two behaviours that actually matter: `Limit` bounds items *read* rather than returned, and
a `FilterExpression` is applied after that read.
"""

from __future__ import annotations

import time
from typing import Any

from guardrail_service.storage.decisions import (
    MAX_QUEUE_PAGES,
    QUEUE_PAGE_SIZE,
    DynamoDBDecisionRepository,
    PendingDecision,
    build_pending,
)


class FakeIndex:
    """A stub DynamoDB that models the parts of `query` this code depends on.

    Deliberately faithful about the one thing the bug turned on: **`Limit` caps the items
    read, and `FilterExpression` is applied to that page afterwards.** A stub that applied
    the filter first would reproduce the in-memory semantics and hide the defect all over
    again.
    """

    def __init__(self, items: list[dict[str, Any]]) -> None:
        # Stored oldest-first, as the index is.
        self.items = items
        self.queries: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(kwargs)

        start = 0
        if (key := kwargs.get("ExclusiveStartKey")) is not None:
            start = int(key["gsi1sk"]["S"])

        page_size = kwargs.get("Limit", len(self.items))
        page = self.items[start : start + page_size]
        read_to = start + len(page)

        if expression := kwargs.get("FilterExpression"):
            assert expression == "decision_expires_at > :now", expression
            now = int(kwargs["ExpressionAttributeValues"][":now"]["N"])
            page = [i for i in page if int(i["decision_expires_at"]["N"]) > now]

        response: dict[str, Any] = {"Items": page}
        if read_to < len(self.items):
            response["LastEvaluatedKey"] = {"gsi1sk": {"S": str(read_to)}}
        return response


def _item(decision_id: str, *, expires_in: int) -> dict[str, Any]:
    """One index entry, serialized the way the repository expects to read it."""
    decision = build_pending(
        decision_id=decision_id,
        tenant_id="acme",
        tool="email.send",
        arguments={"to": ["x@external.com"]},
        agent_id="bot",
        session_id="s",
        matched_rules=[{"rule_id": "external-email-review"}],
        message="held",
        audit_seq=1,
        timeout_seconds=max(expires_in, 1),
        on_timeout="deny",
        reviewers=[],
    )
    item = decision.to_item()
    # build_pending computes expiry from *now*; override it so a record can be placed in
    # the past, which is the whole point of the fixture.
    item["decision_expires_at"] = int(time.time()) + expires_in

    serialized: dict[str, Any] = {}
    for key, value in item.items():
        if isinstance(value, bool):
            serialized[key] = {"BOOL": value}
        elif isinstance(value, int):
            serialized[key] = {"N": str(value)}
        elif isinstance(value, list):
            serialized[key] = {"L": [{"S": str(v)} for v in value]}
        else:
            serialized[key] = {"S": str(value)}
    return serialized


def _repository(items: list[dict[str, Any]]) -> tuple[DynamoDBDecisionRepository, FakeIndex]:
    client = FakeIndex(items)
    return DynamoDBDecisionRepository("guardrail-audit-test", client=client), client


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------


def test_a_live_decision_is_found_behind_a_page_of_expired_ones() -> None:
    """**The defect, reproduced.**

    Sixty expired decisions ahead of one live one -- the shape of the live table, where
    111 stale entries had accumulated. The old implementation read the oldest 50, filtered
    every one of them away, and returned an empty queue while a real action waited.
    """
    items = [_item(f"stale-{n}", expires_in=-3600) for n in range(60)]
    items.append(_item("live-one", expires_in=600))
    repository, _ = _repository(items)

    pending = repository.list_pending("acme", limit=50)

    assert [d.decision_id for d in pending] == ["live-one"], (
        "the live decision was not found behind the expired ones -- the queue is "
        "filtering after limiting again"
    )


def test_expired_decisions_never_appear() -> None:
    """They are unanswerable. Showing a reviewer a request they cannot action wastes the
    attention the queue exists to direct."""
    items = [_item(f"stale-{n}", expires_in=-1) for n in range(5)]
    repository, _ = _repository(items)

    assert repository.list_pending("acme") == []


def test_expiry_is_excluded_in_the_query_not_afterwards() -> None:
    """Filtering server-side is what stops stale entries consuming the result budget.

    Asserted on the request rather than the result: a version that fetched everything and
    filtered in Python would still return the right rows here, and would still fall over
    on a real table.
    """
    repository, client = _repository([_item("a", expires_in=600)])

    repository.list_pending("acme")

    assert client.queries[0]["FilterExpression"] == "decision_expires_at > :now"
    assert ":now" in client.queries[0]["ExpressionAttributeValues"]


# ---------------------------------------------------------------------------
# Ordering and bounds
# ---------------------------------------------------------------------------


def test_the_longest_waiting_decision_comes_first() -> None:
    """Oldest-first: the one closest to timing out into a deny is the one a reviewer
    should see at the top."""
    items = [_item(f"live-{n}", expires_in=600 + n) for n in range(4)]
    repository, client = _repository(items)

    pending = repository.list_pending("acme")

    assert [d.decision_id for d in pending] == ["live-0", "live-1", "live-2", "live-3"]
    # Asserted on the request as well: the ordering above would also hold if the code
    # sorted after reading, and that would not survive a page boundary.
    assert client.queries[0]["ScanIndexForward"] is True


def test_the_limit_is_honoured() -> None:
    items = [_item(f"live-{n}", expires_in=600) for n in range(30)]
    repository, _ = _repository(items)

    assert len(repository.list_pending("acme", limit=10)) == 10


def test_it_stops_paginating_once_it_has_enough() -> None:
    """A full first page must not trigger a second round trip. The queue is on a table
    provisioned at 5 read units."""
    items = [_item(f"live-{n}", expires_in=600) for n in range(QUEUE_PAGE_SIZE * 3)]
    repository, client = _repository(items)

    repository.list_pending("acme", limit=5)

    assert len(client.queries) == 1


def test_pagination_is_bounded() -> None:
    """Without a ceiling, a tenant with a week of abandoned decisions would turn one
    reviewer's page load into an unbounded scan of them."""
    items = [_item(f"stale-{n}", expires_in=-1) for n in range(QUEUE_PAGE_SIZE * 50)]
    repository, client = _repository(items)

    assert repository.list_pending("acme") == []
    assert len(client.queries) == MAX_QUEUE_PAGES


def test_an_empty_index_is_not_an_error() -> None:
    repository, _ = _repository([])

    assert repository.list_pending("acme") == []


# ---------------------------------------------------------------------------
# The two implementations must agree
# ---------------------------------------------------------------------------


def test_both_repositories_agree_on_the_same_queue() -> None:
    """The defect existed because they did not.

    `InMemoryDecisionRepository` filters then limits; the DynamoDB one limited then
    filtered. Both satisfy the type signature. Only one was tested.
    """
    from guardrail_service.storage.decisions import InMemoryDecisionRepository

    live = build_pending(
        decision_id="live-one",
        tenant_id="acme",
        tool="email.send",
        arguments={},
        agent_id="bot",
        session_id="s",
        matched_rules=[],
        message=None,
        audit_seq=1,
        timeout_seconds=600,
        on_timeout="deny",
        reviewers=[],
    )
    expired = [
        PendingDecision(**{**live.__dict__, "decision_id": f"stale-{n}", "expires_at": 1})
        for n in range(60)
    ]

    memory = InMemoryDecisionRepository()
    for decision in [*expired, live]:
        memory.create(decision)

    dynamo, _ = _repository(
        [_item(f"stale-{n}", expires_in=-3600) for n in range(60)]
        + [_item("live-one", expires_in=600)]
    )

    assert [d.decision_id for d in memory.list_pending("acme")] == [
        d.decision_id for d in dynamo.list_pending("acme")
    ]
