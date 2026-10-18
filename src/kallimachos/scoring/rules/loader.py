"""Compile YAML profile rules into :class:`Rule` objects.

``when`` is a predicate tree. Leaves::

    title_regex: "<re>"     abstract_regex: "<re>"     text_regex: "<re>"  (title + abstract)
    field_class: physics | [math, math_phys]
    not_channel_core: [A, B]       # no core phrase of these channels matched
    not_author_status: verified    # no followed author of that status among the authors
    context_channel: C             # true when the main channel is C
    venue_legit: true | false

Combinators: ``all: [...]``, ``any: [...]``, ``not: {...}``. Every regex is compiled once, with
``re.IGNORECASE`` unless it starts with ``(?-i)``. Unknown keys are a :class:`UsageError` (exit 2).
Field-specific policy belongs in these rules, never in ``scoring.rules.core``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from kallimachos.interfaces import (
    Candidate,
    FieldClass,
    Rule,
    RuleContext,
    RuleStage,
    UsageError,
    Verdict,
    VerdictKind,
)
from kallimachos.profile.schema import Profile, ProfileRule
from kallimachos.scoring.candidate import TRACE_CORE, trace_values
from kallimachos.scoring.topic import author_matches

Predicate = Callable[[Candidate, RuleContext], bool]

SUPPORTED_WHEN_KEYS: frozenset[str] = frozenset(
    {
        "title_regex",
        "abstract_regex",
        "text_regex",
        "field_class",
        "not_channel_core",
        "not_author_status",
        "context_channel",
        "venue_legit",
        "all",
        "any",
        "not",
    }
)

_AUTHOR_STATUSES = frozenset({"verified", "candidate", "uncertain", "not_found"})


def _compile_regex(value: Any, key: str) -> re.Pattern[str]:
    if not isinstance(value, str) or not value:
        raise UsageError(f"rule key {key!r} expects a non-empty regex string")
    flags = 0 if value.startswith("(?-i)") else re.IGNORECASE
    pattern = value[5:] if value.startswith("(?-i)") else value
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise UsageError(f"rule key {key!r}: invalid regex {value!r} ({exc})") from exc


def _str_list(value: Any, key: str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise UsageError(f"rule key {key!r} expects a string or a list of strings")


def _field_classes(value: Any) -> frozenset[FieldClass]:
    out: set[FieldClass] = set()
    for item in _str_list(value, "field_class"):
        try:
            out.add(FieldClass(item))
        except ValueError:
            allowed = ", ".join(m.value for m in FieldClass)
            raise UsageError(f"field_class {item!r} unknown (allowed: {allowed})") from None
    return frozenset(out)


def _channel_core_matched(cand: Candidate, channel_id: str) -> bool:
    prefix = f"{channel_id}/"
    return any(v.startswith(prefix) for v in trace_values(cand, TRACE_CORE))


def _has_author_with_status(cand: Candidate, ctx: RuleContext, status: str) -> bool:
    for name in cand.authors:
        for f in ctx.profile.authors.follow:
            if f.status == status and author_matches(
                name, name=f.name, orcid=f.orcid, openalex_id=f.openalex_id
            ):
                return True
    return False


def _venue_legit(cand: Candidate, ctx: RuleContext) -> bool:
    # imported lazily: rules.core imports this module
    from kallimachos.scoring.rules.core import candidate_venue_legit

    return candidate_venue_legit(cand, ctx)[0]


def _leaf(key: str, value: Any) -> Predicate:
    if key == "title_regex":
        rx = _compile_regex(value, key)
        return lambda cand, ctx: rx.search(cand.title or "") is not None
    if key == "abstract_regex":
        rx = _compile_regex(value, key)
        return lambda cand, ctx: rx.search(cand.abstract or "") is not None
    if key == "text_regex":
        rx = _compile_regex(value, key)
        return lambda cand, ctx: rx.search(f"{cand.title or ''}\n{cand.abstract or ''}") is not None
    if key == "field_class":
        classes = _field_classes(value)
        return lambda cand, ctx: cand.field_class in classes
    if key == "not_channel_core":
        channels = _str_list(value, key)
        return lambda cand, ctx: not any(_channel_core_matched(cand, ch) for ch in channels)
    if key == "not_author_status":
        statuses = _str_list(value, key)
        unknown = [s for s in statuses if s not in _AUTHOR_STATUSES]
        if unknown:
            raise UsageError(f"not_author_status: unknown status {unknown[0]!r}")
        return lambda cand, ctx: not any(_has_author_with_status(cand, ctx, s) for s in statuses)
    if key == "context_channel":
        channels = _str_list(value, key)
        return lambda cand, ctx: bool(cand.channels) and cand.channels[0] in channels
    if key == "venue_legit":
        if not isinstance(value, bool):
            raise UsageError("rule key 'venue_legit' expects true or false")
        return lambda cand, ctx: _venue_legit(cand, ctx) is value
    if key == "all":
        parts = _compile_list(value, key)
        return lambda cand, ctx: all(p(cand, ctx) for p in parts)
    if key == "any":
        parts = _compile_list(value, key)
        return lambda cand, ctx: any(p(cand, ctx) for p in parts)
    if key == "not":
        if not isinstance(value, dict):
            raise UsageError("rule key 'not' expects a mapping")
        inner = compile_when(value)
        return lambda cand, ctx: not inner(cand, ctx)
    raise UsageError(f"unknown rule key {key!r} in 'when' (allowed: {sorted(SUPPORTED_WHEN_KEYS)})")


def _compile_list(value: Any, key: str) -> list[Predicate]:
    if not isinstance(value, list) or not value:
        raise UsageError(f"rule key {key!r} expects a non-empty list of conditions")
    out: list[Predicate] = []
    for item in value:
        if not isinstance(item, dict):
            raise UsageError(f"rule key {key!r}: every condition must be a mapping")
        out.append(compile_when(item))
    return out


def compile_when(when: dict[str, Any]) -> Predicate:
    """Unknown keys are a ``UsageError`` (exit 2). Several keys in one mapping are
    combined with AND."""
    if not isinstance(when, dict):
        raise UsageError("rule 'when' must be a mapping")
    unknown = sorted(set(when) - SUPPORTED_WHEN_KEYS)
    if unknown:
        raise UsageError(
            f"unknown rule key {unknown[0]!r} in 'when' (allowed: {sorted(SUPPORTED_WHEN_KEYS)})"
        )
    if not when:
        raise UsageError("rule 'when' must not be empty")
    leaves = [_leaf(key, value) for key, value in when.items()]
    if len(leaves) == 1:
        return leaves[0]
    return lambda cand, ctx: all(p(cand, ctx) for p in leaves)


class YamlRule:
    """A compiled profile rule. A channel rule only applies to candidates of that channel."""

    def __init__(
        self,
        rule_id: str,
        stage: RuleStage,
        predicate: Predicate,
        reason: str,
        channel_id: str | None,
    ) -> None:
        self.id = rule_id
        self.stage = stage
        self._predicate = predicate
        self._reason = reason
        self.channel_id = channel_id

    def apply(self, cand: Candidate, ctx: RuleContext) -> Verdict:
        if self.channel_id is not None and self.channel_id not in cand.channels:
            return Verdict(VerdictKind.NEUTRAL, f"not a {self.channel_id} candidate")
        if not self._predicate(cand, ctx):
            return Verdict.neutral()
        if self.stage is RuleStage.DROP:
            return Verdict(VerdictKind.DROP, self._reason)
        if self.stage is RuleStage.PASS:
            cand.certain_pass = True
            cand.certain_pass_reason = self._reason
            return Verdict(VerdictKind.PASS, self._reason)
        return Verdict(VerdictKind.NEUTRAL, self._reason)


def rule_from_profile(rule: ProfileRule, *, channel_id: str | None) -> Rule:
    scope = f"channel {channel_id}" if channel_id else "profile"
    try:
        predicate = compile_when(rule.when)
    except UsageError as exc:
        raise UsageError(f"rule {rule.id!r} ({scope}): {exc}") from exc
    reason = rule.reason or rule.id
    return YamlRule(rule.id, rule.stage, predicate, reason, channel_id)


def rules_from_profile(profile: Profile) -> list[Rule]:
    """Channel rules then profile-wide rules, in file order."""
    out: list[Rule] = []
    seen_ids: set[str] = set()
    for channel in profile.channels:
        for rule in channel.rules:
            _check_duplicate(rule.id, seen_ids)
            out.append(rule_from_profile(rule, channel_id=channel.id))
    for rule in profile.rules:
        _check_duplicate(rule.id, seen_ids)
        out.append(rule_from_profile(rule, channel_id=None))
    return out


def _check_duplicate(rule_id: str, seen_ids: set[str]) -> None:
    if rule_id in seen_ids:
        raise UsageError(f"duplicate rule id {rule_id!r} in profile")
    seen_ids.add(rule_id)


__all__ = [
    "SUPPORTED_WHEN_KEYS",
    "YamlRule",
    "compile_when",
    "rule_from_profile",
    "rules_from_profile",
]
