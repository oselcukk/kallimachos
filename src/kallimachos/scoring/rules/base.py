"""Rule chain runner.

Order of the chain is fixed by the caller (:func:`kallimachos.scoring.rules.core.default_rules`):
core drop rules, profile YAML rules, certain pass. Exemptions are not a rule but a property of
the context: a candidate whose id is in ``ctx.exempt`` (always-show author, explicit user
request, user "keep") skips every ``drop`` rule, and that skip is itself written to the trace
so that no decision is invisible.
"""

from __future__ import annotations

from collections.abc import Sequence

from kallimachos.interfaces import (
    Candidate,
    Rule,
    RuleContext,
    RuleStage,
    RuleTraceEntry,
    Verdict,
    VerdictKind,
)

EXEMPT_REASON = "exempt: drop rules skipped"


class BaseRule:
    id: str = ""
    stage: RuleStage = RuleStage.ANNOTATE

    def apply(self, cand: Candidate, ctx: RuleContext) -> Verdict:
        """Default: no opinion. Subclasses override."""
        return Verdict.neutral()


def is_exempt(cand: Candidate, ctx: RuleContext) -> bool:
    return cand.always_show_author or cand.id in ctx.exempt


def _trace(cand: Candidate, rule_id: str, verdict: Verdict) -> None:
    cand.rule_trace.append(
        RuleTraceEntry(rule=rule_id, verdict=verdict.kind, reason=verdict.reason)
    )


def run_chain(cand: Candidate, ctx: RuleContext, rules: Sequence[Rule]) -> Verdict:
    """Apply rules in order, append every verdict to ``cand.rule_trace``. Exempt candidates skip
    drop rules. First ``drop`` wins; otherwise the last ``pass``; otherwise neutral."""
    exempt = is_exempt(cand, ctx)
    last_pass: Verdict | None = None
    for rule in rules:
        if rule.stage is RuleStage.DROP and exempt:
            _trace(cand, rule.id, Verdict(VerdictKind.NEUTRAL, EXEMPT_REASON))
            continue
        verdict = rule.apply(cand, ctx)
        if verdict.kind is VerdictKind.DROP and rule.stage is not RuleStage.DROP:
            # an annotate/pass rule may not drop (venue quality never drops alone)
            verdict = Verdict(VerdictKind.NEUTRAL, f"ignored drop from {rule.stage} rule")
        _trace(cand, rule.id, verdict)
        if verdict.kind is VerdictKind.DROP:
            return verdict
        if verdict.kind is VerdictKind.PASS:
            last_pass = verdict
    return last_pass if last_pass is not None else Verdict.neutral()
