"""Policy engine behaviour.

The first block encodes the problem statement's success criteria verbatim. The rest
covers the properties that make the engine trustworthy: order independence, fail-closed
handling of unknowns, and the absence of any code-execution path from a policy file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from guardrail_core.effects import Effect
from guardrail_core.engine import evaluate, render_message
from guardrail_core.models import ActionEnvelope
from guardrail_core.operators import PolicyError
from guardrail_core.policy import PolicyBundle, load_bundle_yaml

POLICY_PATH = Path(__file__).resolve().parents[2] / "policies" / "default.yaml"


@pytest.fixture(scope="module")
def bundle() -> PolicyBundle:
    return load_bundle_yaml(POLICY_PATH.read_text(encoding="utf-8"))


def _decide(bundle: PolicyBundle, tool: str, arguments: dict[str, Any], **kw: Any) -> Any:
    envelope = ActionEnvelope(
        agent_id="test-agent",
        session_id="test-session",
        tool=tool,
        arguments=arguments,
        **kw,
    )
    return evaluate(envelope, bundle)


# ---------------------------------------------------------------------------
# The problem statement's success criteria
# ---------------------------------------------------------------------------


def test_bulk_delete_of_500_records_is_blocked(bundle: PolicyBundle) -> None:
    result = _decide(bundle, "db.delete_records", {"table": "users", "count": 500})

    assert result.effect is Effect.BLOCK
    assert "db-bulk-delete" in [m.rule_id for m in result.matched_rules]
    assert result.allows_execution is False


def test_delete_of_five_records_is_allowed(bundle: PolicyBundle) -> None:
    result = _decide(bundle, "db.delete_records", {"table": "users", "count": 5})

    assert result.effect is Effect.ALLOW
    assert result.matched_rules == []
    assert result.allows_execution is True


def test_email_to_external_domain_requires_human_review(bundle: PolicyBundle) -> None:
    result = _decide(bundle, "email.send", {"to": "partner@external.com"})

    assert result.effect is Effect.REQUIRE_HITL
    assert "external-email-review" in [m.rule_id for m in result.matched_rules]
    assert result.allows_execution is False


def test_internal_email_goes_through(bundle: PolicyBundle) -> None:
    result = _decide(bundle, "email.send", {"to": "colleague@acme-corp.com"})

    assert result.effect is Effect.ALLOW


def test_confidential_path_read_is_logged_and_allowed(bundle: PolicyBundle) -> None:
    result = _decide(bundle, "file.read", {"path": "/srv/confidential/q3.pdf"})

    assert result.effect is Effect.LOG_AND_ALLOW
    assert result.allows_execution is True, "log_and_allow must still execute"
    assert "confidential-read-audit" in [m.rule_id for m in result.matched_rules]


# ---------------------------------------------------------------------------
# Fail-closed behaviour
# ---------------------------------------------------------------------------


def test_delete_with_an_unmeasurable_where_clause_is_blocked(bundle: PolicyBundle) -> None:
    """The row count of `DELETE ... WHERE` is unknowable without running it.

    Rather than assume it is small, the extractor returns UNKNOWN and the block rule
    treats that as matching. Assuming "probably few" is how a guardrail silently stops
    guarding.
    """
    result = _decide(bundle, "db.delete_records", {"table": "users", "where": "1=1"})

    assert result.effect is Effect.BLOCK
    assert "derived.record_count" in result.unknown_paths


def test_unknown_does_not_satisfy_a_permissive_rule() -> None:
    """Permission is never granted on the strength of something we could not determine."""
    permissive = load_bundle_yaml(
        """
        apiVersion: guardrail/v1
        defaults: {effect: block}
        rules:
          - id: small-deletes-ok
            match:
              tool: db.delete_records
              all: [{path: derived.record_count, op: lt, value: 10}]
            effect: allow
        """
    )

    result = _decide(permissive, "db.delete_records", {"where": "1=1"})

    assert result.effect is Effect.BLOCK, "an unknown count must not unlock the allow rule"


def test_bcc_recipients_are_inspected(bundle: PolicyBundle) -> None:
    """A rule that only read `to` would let data leave via `bcc` -- exactly the gap
    this project exists to close."""
    result = _decide(
        bundle,
        "email.send",
        {"to": "colleague@acme-corp.com", "bcc": ["exfil@external.com"]},
    )

    assert result.effect is Effect.REQUIRE_HITL


def test_unparseable_recipients_are_unknown_not_empty(bundle: PolicyBundle) -> None:
    """A send whose addresses cannot be parsed must not read as 'no external recipients'."""
    result = _decide(bundle, "email.send", {"to": "not-an-address"})

    assert result.effect is Effect.REQUIRE_HITL
    assert "derived.recipient_domains" in result.unknown_paths


# ---------------------------------------------------------------------------
# Resolution semantics
# ---------------------------------------------------------------------------


def test_most_restrictive_effect_wins_over_rule_order() -> None:
    """Reordering a policy file must never change an outcome.

    Under first-match-wins, moving a broad allow above a narrow block silently disables
    the block -- no error, and a diff that looks harmless.
    """
    rules_block_first = """
        apiVersion: guardrail/v1
        rules:
          - id: deny-it
            match: {tool: db.drop_table}
            effect: block
          - id: permit-it
            match: {tool: db.drop_table}
            effect: log_and_allow
    """
    rules_allow_first = """
        apiVersion: guardrail/v1
        rules:
          - id: permit-it
            match: {tool: db.drop_table}
            effect: log_and_allow
          - id: deny-it
            match: {tool: db.drop_table}
            effect: block
    """

    first = _decide(load_bundle_yaml(rules_block_first), "db.drop_table", {})
    second = _decide(load_bundle_yaml(rules_allow_first), "db.drop_table", {})

    assert first.effect is Effect.BLOCK
    assert second.effect is Effect.BLOCK


def test_all_matching_rules_are_recorded_not_only_the_winner(bundle: PolicyBundle) -> None:
    """A reviewer needs to see everything an action tripped, not just what dominated."""
    result = _decide(
        bundle,
        "db.delete_records",
        {"count": 500, "table": "users"},
        context={"environment": "production"},
    )

    matched = {m.rule_id for m in result.matched_rules}
    assert {"db-bulk-delete", "destructive-tool-in-production"} <= matched
    assert result.effect is Effect.BLOCK


def test_unmatched_action_falls_back_to_bundle_default(bundle: PolicyBundle) -> None:
    result = _decide(bundle, "calendar.list_events", {"range": "week"})

    assert result.effect is Effect.ALLOW
    assert result.matched_rules == []


def test_default_deny_bundle_blocks_unmatched_actions() -> None:
    strict = load_bundle_yaml(
        """
        apiVersion: guardrail/v1
        defaults: {effect: block}
        rules:
          - id: allow-reads
            match: {tool: file.read}
            effect: allow
        """
    )

    assert _decide(strict, "file.read", {"path": "/tmp/x"}).effect is Effect.ALLOW
    assert _decide(strict, "db.delete_records", {"count": 1}).effect is Effect.BLOCK


def test_tool_glob_matches_a_family_of_tools(bundle: PolicyBundle) -> None:
    """`file.*` must cover file.read and file.write without enumerating them."""
    for tool in ("file.read", "file.write", "file.delete"):
        result = _decide(bundle, tool, {"path": "/home/app/.ssh/id_rsa"})
        assert result.effect is Effect.BLOCK, tool


def test_disabled_rules_do_not_fire() -> None:
    disabled = load_bundle_yaml(
        """
        apiVersion: guardrail/v1
        rules:
          - id: off-rule
            match: {tool: db.delete_records}
            effect: block
            enabled: false
        """
    )

    assert _decide(disabled, "db.delete_records", {"count": 999}).effect is Effect.ALLOW


# ---------------------------------------------------------------------------
# Decision provenance
# ---------------------------------------------------------------------------


def test_result_pins_the_bundle_version(bundle: PolicyBundle) -> None:
    """A decision must be reproducible against the policy in force when it was made."""
    result = _decide(bundle, "db.delete_records", {"count": 500})

    assert result.bundle_id == "default"
    assert result.bundle_version == bundle.metadata.version


def test_evaluation_is_deterministic(bundle: PolicyBundle) -> None:
    """Same input, same bundle, same answer -- required of anything used as evidence."""
    args = {"to": ["a@acme-corp.com", "b@external.com"], "subject": "q3"}

    results = [_decide(bundle, "email.send", args) for _ in range(20)]

    assert len({r.effect for r in results}) == 1
    assert len({tuple(m.rule_id for m in r.matched_rules) for r in results}) == 1


def test_message_interpolates_derived_facts(bundle: PolicyBundle) -> None:
    result = _decide(bundle, "db.delete_records", {"count": 500})

    assert result.message is not None
    assert "500" in result.message


def test_message_rendering_cannot_reach_attributes() -> None:
    """Rendering must not use str.format, which exposes attribute and index access on
    arbitrary objects -- the same hazard class as eval on the operator set."""
    facts = {"args": {"name": "value"}}

    rendered = render_message("{args.__class__} and {args.name}", facts)

    assert rendered is not None
    assert "__class__" in rendered, "the placeholder should be left literal, not resolved"
    assert "type" not in rendered
    assert "value" in rendered


# ---------------------------------------------------------------------------
# Policy validation happens at load time, never mid-evaluation
# ---------------------------------------------------------------------------


def test_unknown_operator_is_rejected_at_load_time() -> None:
    with pytest.raises(PolicyError, match="unknown operator"):
        load_bundle_yaml(
            """
            apiVersion: guardrail/v1
            rules:
              - id: bad-op
                match: {tool: x, all: [{path: args.a, op: regex_match, value: y}]}
                effect: block
            """
        )


def test_unknown_derived_fact_is_rejected_at_load_time() -> None:
    """A typo'd derived path would silently never match -- coverage that isn't there."""
    with pytest.raises(PolicyError, match="unknown derived fact"):
        load_bundle_yaml(
            """
            apiVersion: guardrail/v1
            rules:
              - id: bad-fact
                match: {tool: x, all: [{path: derived.recrd_count, op: gt, value: 1}]}
                effect: block
            """
        )


def test_invalid_regex_is_rejected_at_load_time() -> None:
    with pytest.raises(PolicyError, match="invalid regex"):
        load_bundle_yaml(
            """
            apiVersion: guardrail/v1
            rules:
              - id: bad-regex
                match: {tool: x, all: [{path: args.a, op: matches, value: "([unclosed"}]}
                effect: block
            """
        )


def test_duplicate_rule_ids_are_rejected() -> None:
    """Ids appear in audit records, so they must identify exactly one rule."""
    with pytest.raises(PolicyError, match="duplicate rule id"):
        load_bundle_yaml(
            """
            apiVersion: guardrail/v1
            rules:
              - id: same-id
                match: {tool: a}
                effect: block
              - id: same-id
                match: {tool: b}
                effect: allow
            """
        )


def test_rule_matching_everything_is_rejected() -> None:
    with pytest.raises(PolicyError):
        load_bundle_yaml(
            """
            apiVersion: guardrail/v1
            rules:
              - id: catch-all
                match: {}
                effect: block
            """
        )


def test_hitl_settings_on_a_non_hitl_rule_are_rejected() -> None:
    """Silently ignored configuration is worse than a rejected file: the author believes
    a timeout applies when it does not."""
    with pytest.raises(PolicyError, match="hitl"):
        load_bundle_yaml(
            """
            apiVersion: guardrail/v1
            rules:
              - id: confused
                match: {tool: a}
                effect: block
                hitl: {timeout_seconds: 60}
            """
        )


def test_yaml_cannot_construct_python_objects() -> None:
    """safe_load, never load. Full YAML can instantiate arbitrary classes, which is the
    same vulnerability class this engine avoids by not using eval."""
    with pytest.raises(PolicyError):
        load_bundle_yaml(
            """
            apiVersion: guardrail/v1
            rules: !!python/object/apply:os.system ["echo pwned"]
            """
        )
