"""The evaluation engine.

Pure: `evaluate(envelope, bundle)` is a function of its inputs. No network, no clock, no
filesystem, no randomness. Three properties follow, and all three matter for a system
whose output is used as evidence:

* the same action and bundle always produce the same decision
* a past decision can be reproduced exactly, given the bundle version recorded with it
* the whole engine is testable offline, with no AWS and no mocks
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping
from typing import Any

from guardrail_core.effects import Effect
from guardrail_core.extractors import derive_facts
from guardrail_core.models import ActionEnvelope, EvaluationResult, RuleMatch
from guardrail_core.operators import UNKNOWN, apply_operator
from guardrail_core.policy import Match, PolicyBundle, Predicate, Rule

_MISSING = object()


def _resolve_path(path: str, facts: Mapping[str, Any]) -> Any:
    """Walk a dotted path through the fact tree.

    Returns None for a path that is genuinely absent -- distinct from UNKNOWN, which
    means "this exists conceptually but could not be determined". `args.nickname` missing
    is an absence; `derived.record_count` on a `WHERE` clause is an unknown. Conflating
    them would make `exists` meaningless.
    """
    current: Any = facts
    for part in path.split("."):
        if current is UNKNOWN:
            return UNKNOWN
        if isinstance(current, Mapping):
            current = current.get(part, _MISSING)
        else:
            current = getattr(current, part, _MISSING)
        if current is _MISSING:
            return None
    return current


def build_facts(envelope: ActionEnvelope) -> dict[str, Any]:
    """Assemble everything a predicate can address.

    Built once per evaluation and reused across all rules, and recorded in the audit log
    so a past decision can be explained in terms of what the engine actually believed.
    """
    return {
        "tool": envelope.tool,
        "tenant_id": envelope.tenant_id,
        "agent_id": envelope.agent_id,
        "args": envelope.arguments,
        "derived": derive_facts(envelope.arguments),
        "context": envelope.context,
        "principal": envelope.principal.model_dump() if envelope.principal else {},
    }


def _tool_matches(pattern: str | None, tool: str) -> bool:
    """Match a tool name, supporting globs like `db.*`."""
    if pattern is None:
        return True
    if any(ch in pattern for ch in "*?["):
        return fnmatch.fnmatchcase(tool, pattern)
    return pattern == tool


def _evaluate_predicate(
    predicate: Predicate,
    facts: Mapping[str, Any],
    *,
    effect: Effect,
    unknown_paths: list[str],
) -> bool:
    """Evaluate one predicate, recording any UNKNOWN it encountered."""
    actual = _resolve_path(predicate.path, facts)

    if actual is UNKNOWN and predicate.path not in unknown_paths:
        unknown_paths.append(predicate.path)

    return apply_operator(predicate.op, actual, predicate.value, effect=effect)


def _match_applies(
    match: Match,
    facts: Mapping[str, Any],
    tool: str,
    *,
    effect: Effect,
    unknown_paths: list[str],
) -> bool:
    """Whether a rule's match block applies to this action."""
    if not _tool_matches(match.tool, tool):
        return False

    if match.all and not all(
        _evaluate_predicate(p, facts, effect=effect, unknown_paths=unknown_paths) for p in match.all
    ):
        return False

    # Written as a guard rather than one inlined boolean: each clause is a distinct
    # reason a rule does not apply, and keeping them separate keeps it debuggable.
    if match.any and not any(  # noqa: SIM103
        _evaluate_predicate(p, facts, effect=effect, unknown_paths=unknown_paths) for p in match.any
    ):
        return False

    return True


def render_message(template: str | None, facts: Mapping[str, Any]) -> str | None:
    """Interpolate `{derived.x}` / `{args.y}` placeholders in a rule message.

    Deliberately not `str.format`, which would expose attribute and index access on
    arbitrary objects -- the same class of hazard as `eval` on the operator set. This
    only substitutes resolved fact paths, and leaves an unresolvable placeholder as
    written so the defect is visible rather than crashing an evaluation.
    """
    if not template:
        return None

    result = template
    for placeholder in _iter_placeholders(template):
        value = _resolve_path(placeholder, facts)
        if value is UNKNOWN:
            rendered = "unknown"
        elif value is None:
            continue
        elif isinstance(value, (list, tuple)):
            rendered = ", ".join(str(item) for item in value)
        else:
            rendered = str(value)
        result = result.replace("{" + placeholder + "}", rendered)

    return result


def _iter_placeholders(template: str) -> list[str]:
    """Extract `{path}` placeholders, ignoring anything that is not a fact path."""
    import re

    return [
        m.group(1) for m in re.finditer(r"\{([a-z_][a-z0-9_]*(?:\.[a-zA-Z0-9_-]+)*)\}", template)
    ]


def evaluate(envelope: ActionEnvelope, bundle: PolicyBundle) -> EvaluationResult:
    """Decide what should happen to one tool call.

    **Every** enabled rule is evaluated, not merely up to the first match, and the
    strictest matching effect wins. That makes the outcome independent of rule order in
    the file -- under first-match-wins, moving a broad `allow` above a narrow `block`
    silently disables the block, with no error and no diff that looks dangerous.
    """
    facts = build_facts(envelope)
    unknown_paths: list[str] = []
    matches: list[RuleMatch] = []

    for rule in bundle.active_rules:
        if _match_applies(
            rule.match,
            facts,
            envelope.tool,
            effect=rule.effect,
            unknown_paths=unknown_paths,
        ):
            matches.append(
                RuleMatch(
                    rule_id=rule.id,
                    effect=rule.effect,
                    severity=rule.severity,
                    message=render_message(rule.message, facts),
                )
            )

    effect = (
        Effect.most_restrictive([m.effect for m in matches]) if matches else bundle.defaults.effect
    )

    # The explanation comes from the rule that actually determined the outcome. Among
    # equally-strict matches the first in file order wins, purely so the message is
    # deterministic.
    winner = next((m for m in matches if m.effect == effect), None)

    return EvaluationResult(
        effect=effect,
        matched_rules=matches,
        message=winner.message if winner else None,
        bundle_id=bundle.metadata.bundle_id,
        bundle_version=bundle.metadata.version,
        unknown_paths=unknown_paths,
    )


def winning_rule(result: EvaluationResult, bundle: PolicyBundle) -> Rule | None:
    """The rule that produced the outcome, for HITL settings such as timeout."""
    for match in result.matched_rules:
        if match.effect == result.effect:
            return next((r for r in bundle.rules if r.id == match.rule_id), None)
    return None
