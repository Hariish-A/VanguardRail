"""Property-based tests on the evaluator.

Example-based tests confirm the cases someone thought of. These assert invariants that
must hold for *every* input, which is the right tool for a component whose output is
used as evidence. Hypothesis generates the awkward cases -- empty strings, huge numbers,
unicode, deeply nested arguments -- that hand-written examples reliably miss.
"""

from __future__ import annotations

from typing import Any

from guardrail_core.effects import Effect
from guardrail_core.engine import evaluate
from guardrail_core.models import ActionEnvelope
from guardrail_core.policy import load_bundle_yaml
from hypothesis import given, settings
from hypothesis import strategies as st

# Values a tool argument might plausibly hold, including the shapes that have caused
# real bugs: floats, empty strings, and nested structures.
json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(10**12), max_value=10**12)
    | st.floats(allow_nan=False, allow_infinity=False, width=32)
    | st.text(max_size=60),
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(st.text(min_size=1, max_size=12), children, max_size=4)
    ),
    max_leaves=8,
)

arguments = st.dictionaries(st.text(min_size=1, max_size=16), json_values, max_size=6)

tool_names = st.sampled_from(
    [
        "db.delete_records",
        "db.update",
        "email.send",
        "file.read",
        "file.write",
        "payments.refund",
        "http.request",
        "calendar.create",
        "unknown.tool",
    ]
)

BUNDLE = load_bundle_yaml(
    """
    apiVersion: guardrail/v1
    defaults: {effect: allow}
    rules:
      - id: bulk-delete
        match:
          tool: db.delete_records
          all: [{path: derived.record_count, op: gt, value: 100}]
        effect: block
      - id: external-email
        match:
          tool: email.send
          any: [{path: derived.recipient_domains, op: any_not_in, value: [acme-corp.com]}]
        effect: require_hitl
      - id: confidential
        match:
          tool: file.read
          all: [{path: args.path, op: icontains, value: confidential}]
        effect: log_and_allow
      - id: secrets
        match:
          tool: file.*
          any: [{path: args.path, op: icontains, value: id_rsa}]
        effect: block
    """
)


def _envelope(tool: str, args: dict[str, Any]) -> ActionEnvelope:
    return ActionEnvelope(agent_id="a", session_id="s", tool=tool, arguments=args)


@given(tool=tool_names, args=arguments)
@settings(max_examples=300, deadline=None)
def test_evaluation_never_raises(tool: str, args: dict[str, Any]) -> None:
    """The engine must survive any argument shape an agent can produce.

    An exception here would become a 500, and a fail-closed SDK would block a legitimate
    action -- turning a malformed argument into an outage.
    """
    evaluate(_envelope(tool, args), BUNDLE)


@given(tool=tool_names, args=arguments)
@settings(max_examples=300, deadline=None)
def test_result_is_always_a_valid_effect(tool: str, args: dict[str, Any]) -> None:
    result = evaluate(_envelope(tool, args), BUNDLE)

    assert isinstance(result.effect, Effect)
    assert result.allows_execution == (result.effect in (Effect.ALLOW, Effect.LOG_AND_ALLOW))


@given(tool=tool_names, args=arguments)
@settings(max_examples=200, deadline=None)
def test_evaluation_is_deterministic(tool: str, args: dict[str, Any]) -> None:
    """Same input, same answer -- required of anything used as evidence."""
    envelope = _envelope(tool, args)

    first = evaluate(envelope, BUNDLE)
    second = evaluate(envelope, BUNDLE)

    assert first.effect == second.effect
    assert [m.rule_id for m in first.matched_rules] == [m.rule_id for m in second.matched_rules]


@given(tool=tool_names, args=arguments)
@settings(max_examples=200, deadline=None)
def test_outcome_is_the_strictest_matching_rule(tool: str, args: dict[str, Any]) -> None:
    """The resolution invariant, over arbitrary inputs rather than chosen examples."""
    result = evaluate(_envelope(tool, args), BUNDLE)

    if result.matched_rules:
        assert result.effect == max(m.effect for m in result.matched_rules)
    else:
        assert result.effect == BUNDLE.defaults.effect


@given(args=arguments)
@settings(max_examples=200, deadline=None)
def test_rule_order_never_changes_the_outcome(args: dict[str, Any]) -> None:
    """Reordering a policy file must be a no-op.

    Under first-match-wins, moving a broad allow above a narrow block silently disables
    the block. Most-restrictive-wins is order-independent by construction, and this
    asserts it holds for arbitrary inputs rather than one example.
    """
    reversed_bundle = BUNDLE.model_copy(update={"rules": list(reversed(BUNDLE.rules))})

    original = evaluate(_envelope("db.delete_records", args), BUNDLE)
    shuffled = evaluate(_envelope("db.delete_records", args), reversed_bundle)

    assert original.effect == shuffled.effect
    assert {m.rule_id for m in original.matched_rules} == {
        m.rule_id for m in shuffled.matched_rules
    }


@given(count=st.integers(min_value=0, max_value=10**9))
@settings(max_examples=200, deadline=None)
def test_delete_threshold_holds_at_every_magnitude(count: int) -> None:
    """The boundary is exactly 100, at every scale and with no off-by-one."""
    result = evaluate(_envelope("db.delete_records", {"count": count}), BUNDLE)

    assert (result.effect is Effect.BLOCK) == (count > 100)


@given(
    local=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=10),
    domain=st.sampled_from(["evil.com", "partner.org", "external.net", "gmail.com"]),
)
@settings(max_examples=200, deadline=None)
def test_any_external_recipient_always_triggers_review(local: str, domain: str) -> None:
    """However the address is spelled, a non-internal domain must be caught."""
    result = evaluate(_envelope("email.send", {"to": f"{local}@{domain}"}), BUNDLE)

    assert result.effect is Effect.REQUIRE_HITL


@given(
    prefix=st.text(alphabet="abcdefghijklmnopqrstuvwxyz/", max_size=20),
    suffix=st.text(alphabet="abcdefghijklmnopqrstuvwxyz/.", max_size=20),
)
@settings(max_examples=200, deadline=None)
def test_credential_paths_are_blocked_wherever_they_appear(prefix: str, suffix: str) -> None:
    """A rule keyed on a substring must not be evadable by padding the path."""
    result = evaluate(_envelope("file.read", {"path": f"{prefix}id_rsa{suffix}"}), BUNDLE)

    assert result.effect is Effect.BLOCK
