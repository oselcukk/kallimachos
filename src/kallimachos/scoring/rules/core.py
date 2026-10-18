"""Core rules.

Context the rules read from ``RuleContext.extra`` (the only extension point without touching
``interfaces.py``):

* ``records``: ``{candidate_id: Record}`` for paratext/repository classification (otherwise a
  minimal record is rebuilt from the candidate);
* ``unshown_grace_days``: ``rescan.unshown_grace_days`` (default 7);
* ``scoring_config``: :class:`ScoringConfig` (defaults when absent);
* ``user_quality``: ``{issn: VenueQuality}`` from ``data/venue_quality.json``;
* ``predatory_venues_extra``: user additions to the packaged predatory list;
* ``paratext_extra`` / ``repository_venues_extra``: ``scoring.*_extra`` config lists.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from kallimachos.config.schema import ScoringConfig
from kallimachos.data import load_json
from kallimachos.interfaces import (
    Candidate,
    Record,
    Rule,
    RuleContext,
    RuleStage,
    VenueIndicator,
    Verdict,
    VerdictKind,
    WorkKind,
)
from kallimachos.profile.schema import Profile
from kallimachos.records import paratext
from kallimachos.scoring.candidate import TRACE_SEED, TRACE_VERIFIED, topic_score_of, trace_values
from kallimachos.scoring.rules.base import BaseRule, is_exempt
from kallimachos.scoring.rules.loader import rules_from_profile
from kallimachos.scoring.seen import SeenDecision, seen_decision
from kallimachos.scoring.topic import author_matches
from kallimachos.scoring.venue_quality import VenueQuality, normalize_issn, venue_legit

PREDATORY_FILE = "predatory_venues.json"
DEFAULT_GRACE_DAYS = 7


def record_for(cand: Candidate, ctx: RuleContext) -> Record:
    records = ctx.extra.get("records")
    if isinstance(records, dict):
        rec = records.get(cand.id)
        if isinstance(rec, Record):
            return rec
    return Record(
        title=cand.title,
        venue=cand.venue,
        doi=cand.doi,
        arxiv_id=cand.arxiv,
        abstract=cand.abstract,
        kind=cand.kind,
        sources=set(cand.sources),
    )


def scoring_config(ctx: RuleContext) -> ScoringConfig:
    cfg = ctx.extra.get("scoring_config")
    return cfg if isinstance(cfg, ScoringConfig) else ScoringConfig()


def _str_list(ctx: RuleContext, key: str) -> list[str]:
    value = ctx.extra.get(key)
    if isinstance(value, list | tuple):
        return [str(v) for v in value]
    return []


class SeenRule(BaseRule):
    id = "seen"
    stage = RuleStage.DROP

    def apply(self, cand: Candidate, ctx: RuleContext) -> Verdict:
        entry = None
        for key in cand.keys:
            entry = ctx.seen.get(key)
            if entry is not None:
                break
        if entry is None:
            return Verdict.neutral()
        grace = ctx.extra.get("unshown_grace_days", DEFAULT_GRACE_DAYS)
        grace_days = grace if isinstance(grace, int) else DEFAULT_GRACE_DAYS
        decision = seen_decision(entry, cand.kind, today=ctx.run_date, grace_days=grace_days)
        if decision is SeenDecision.SHOWN:
            return Verdict(VerdictKind.DROP, f"shown before in {entry.shown_run}")
        if decision is SeenDecision.EXPIRED:
            return Verdict(
                VerdictKind.DROP,
                f"unshown since {entry.first_run}, grace of {grace_days} days over",
            )
        if decision is SeenDecision.JOURNAL_VERSION:
            cand.note = "journal version of a preprint shown earlier"
            return Verdict(
                VerdictKind.NEUTRAL, "preprint shown before; journal version allowed once"
            )
        return Verdict(
            VerdictKind.NEUTRAL, f"unshown candidate within grace (since {entry.first_run})"
        )


class ParatextRule(BaseRule):
    id = "paratext"
    stage = RuleStage.DROP

    def apply(self, cand: Candidate, ctx: RuleContext) -> Verdict:
        kind = paratext.classify(
            record_for(cand, ctx),
            extra_paratext=_str_list(ctx, "paratext_extra"),
            extra_repositories=_str_list(ctx, "repository_venues_extra"),
        )
        if kind == "paratext":
            return Verdict(VerdictKind.DROP, "editorial matter / paratext")
        return Verdict.neutral()


class RepositoryVenueRule(BaseRule):
    id = "repository_venue"
    stage = RuleStage.DROP

    def apply(self, cand: Candidate, ctx: RuleContext) -> Verdict:
        kind = paratext.classify(
            record_for(cand, ctx),
            extra_paratext=_str_list(ctx, "paratext_extra"),
            extra_repositories=_str_list(ctx, "repository_venues_extra"),
        )
        if kind == "repository":
            return Verdict(VerdictKind.DROP, f"generic repository venue: {cand.venue}")
        return Verdict.neutral()


class PredatoryList:
    """Packaged ``predatory_venues.json`` (``kallimachos.predatory_venues/1``) plus user additions.

    The file shape is ``{"entries": [{"name_regex", "doi_prefixes", "issn", ...}]}``; user
    additions (``scoring.predatory_venues_extra``) are names or regexes. A list of names is
    accepted too. A missing file is an empty list.
    """

    def __init__(self, data: Any = None, extra: Iterable[str] = ()) -> None:
        self.patterns: list[re.Pattern[str]] = []
        self.issn: set[str] = set()
        self.doi_prefixes: list[str] = []
        names: list[str] = []
        if isinstance(data, list):
            names.extend(str(x) for x in data)
        elif isinstance(data, dict):
            for entry in data.get("entries", []) or []:
                if not isinstance(entry, dict):
                    continue
                if entry.get("name_regex"):
                    names.append(str(entry["name_regex"]))
                self.issn.update(normalize_issn(str(x)) for x in entry.get("issn", []) or [])
                self.doi_prefixes.extend(str(x) for x in entry.get("doi_prefixes", []) or [])
        names.extend(extra)
        for name in names:
            try:
                self.patterns.append(re.compile(name, re.I))
            except re.error:
                self.patterns.append(re.compile(re.escape(name), re.I))

    @classmethod
    def load(cls, extra: Iterable[str] = ()) -> PredatoryList:
        try:
            data = load_json(PREDATORY_FILE)
        except FileNotFoundError:
            data = []
        return cls(data, extra)

    def matches(self, cand: Candidate) -> str | None:
        venue = cand.venue or ""
        for pattern in self.patterns:
            if venue and pattern.search(venue):
                return f"venue matches predatory list: {pattern.pattern}"
        if cand.doi:
            for prefix in self.doi_prefixes:
                if cand.doi.lower().startswith(prefix.lower()):
                    return f"doi prefix on predatory list: {prefix}"
        return None

    def matches_issn(self, issns: Iterable[str]) -> str | None:
        for issn in issns:
            if normalize_issn(issn) in self.issn:
                return f"issn on predatory list: {issn}"
        return None


def predatory_list(ctx: RuleContext) -> PredatoryList:
    cached = ctx.extra.get("_predatory_list")
    if isinstance(cached, PredatoryList):
        return cached
    lst = PredatoryList.load(_str_list(ctx, "predatory_venues_extra"))
    ctx.extra["_predatory_list"] = lst
    return lst


class PredatoryVenueRule(BaseRule):
    id = "predatory_venue"
    stage = RuleStage.DROP

    def apply(self, cand: Candidate, ctx: RuleContext) -> Verdict:
        hit = predatory_list(ctx).matches(cand)
        if hit:
            return Verdict(VerdictKind.DROP, hit)
        return Verdict.neutral()


def _record_issns(cand: Candidate, ctx: RuleContext) -> list[str]:
    rec = record_for(cand, ctx)
    return [normalize_issn(i) for i in rec.issn]


def user_quality_for(cand: Candidate, ctx: RuleContext) -> VenueQuality | None:
    quality = ctx.extra.get("user_quality")
    if not isinstance(quality, dict):
        return None
    for issn in _record_issns(cand, ctx):
        entry = quality.get(issn)
        if isinstance(entry, VenueQuality):
            return entry
    return None


def indicator_for(cand: Candidate, ctx: RuleContext) -> VenueIndicator:
    for key in _record_issns(cand, ctx):
        for variant in (key, key.lower()):
            if variant in ctx.venue_indicator:
                return ctx.venue_indicator[variant]
    return VenueIndicator.UNKNOWN


class VenueQualityRule(BaseRule):
    """Only active when quality data is loaded; annotates, never drops alone."""

    id = "venue_quality"
    stage = RuleStage.ANNOTATE

    def apply(self, cand: Candidate, ctx: RuleContext) -> Verdict:
        quality = user_quality_for(cand, ctx)
        if quality is not None:
            year = cand.date.year if cand.date else None
            q = quality.quartile_for(year)
            cand.venue_quality_short = (
                "discontinued" if quality.discontinued else (f"Q{q}" if q else "unranked")
            )
            cand.quality_mark = cand.venue_quality_short
            return Verdict(VerdictKind.NEUTRAL, f"user quality: {cand.venue_quality_short}")
        if not ctx.venue_indicator:
            return Verdict.neutral()
        indicator = indicator_for(cand, ctx)
        if indicator is VenueIndicator.UNKNOWN:
            return Verdict.neutral()
        cand.venue_quality_short = indicator.value
        cand.quality_mark = indicator.value
        return Verdict(VerdictKind.NEUTRAL, f"venue indicator: {indicator.value}")


class PriorUserDropRule(BaseRule):
    id = "prior_user_drop"
    stage = RuleStage.DROP

    def apply(self, cand: Candidate, ctx: RuleContext) -> Verdict:
        if cand.id in ctx.prior_user_drops or any(k in ctx.prior_user_drops for k in cand.keys):
            return Verdict(VerdictKind.DROP, "marked irrelevant by the user before")
        return Verdict.neutral()


def is_preprint(cand: Candidate) -> bool:
    return cand.kind is WorkKind.PREPRINT or (bool(cand.arxiv) and not cand.doi)


def followed_journal(cand: Candidate, ctx: RuleContext) -> bool:
    issns = set(_record_issns(cand, ctx))
    if not issns:
        return False
    return any(
        normalize_issn(i) in issns
        for ch in ctx.profile.channels
        for j in ch.journals
        for i in j.issn
    )


def candidate_venue_legit(cand: Candidate, ctx: RuleContext) -> tuple[bool, str]:
    """Preprint servers and followed journals are legit; otherwise :func:`venue_legit`."""
    predatory = predatory_list(ctx).matches(cand) is not None
    if predatory:
        return False, "predatory venue"
    if is_preprint(cand):
        return True, "preprint"
    if followed_journal(cand, ctx):
        return True, "followed journal"
    quality = user_quality_for(cand, ctx)
    indicator = indicator_for(cand, ctx)
    ok = venue_legit(indicator, predatory=False, user_quality=quality)
    label = quality.short() if quality is not None else indicator.value
    return ok, f"venue {label}"


def has_verified_follow_author(cand: Candidate, ctx: RuleContext) -> bool:
    if TRACE_VERIFIED in (e.reason for e in cand.rule_trace if e.rule == "score"):
        return True
    for name in cand.authors:
        for f in ctx.profile.authors.follow:
            if f.status == "verified" and author_matches(name, name=f.name):
                return True
    return False


def seed_citations_of(cand: Candidate) -> int:
    values = trace_values(cand, TRACE_SEED)
    if values:
        try:
            return int(values[-1])
        except ValueError:
            return 0
    return 1 if cand.near_note is not None and cand.near_note.match == "citation" else 0


def topic_score(cand: Candidate) -> float:
    """Pure topic score: from the score trace, else total minus the known fixed bonuses."""
    traced = topic_score_of(cand)
    if traced is not None:
        return traced
    score = cand.score
    if cand.always_show_author:
        score -= 100
    if cand.full_journal:
        score -= 3
    return max(score, 0.0)


class CertainPassRule(BaseRule):
    """Sets ``cand.certain_pass`` when every certain-pass condition holds."""

    id = "certain_pass"
    stage = RuleStage.PASS

    def apply(self, cand: Candidate, ctx: RuleContext) -> Verdict:
        cfg = scoring_config(ctx)
        if is_exempt(cand, ctx) and not cand.always_show_author:
            return Verdict(VerdictKind.NEUTRAL, "exempt by user request: not a certain pass")
        if cand.field_class not in cfg.certain_pass_fields:
            return Verdict(VerdictKind.NEUTRAL, f"field {cand.field_class.value} not allowed")
        if cand.noise:
            return Verdict(VerdictKind.NEUTRAL, "noise terms matched")
        legit, venue_note = candidate_venue_legit(cand, ctx)
        if not legit:
            return Verdict(VerdictKind.NEUTRAL, f"venue not legit ({venue_note})")
        topic = topic_score(cand)
        threshold = cand.threshold
        main = cand.channels[0] if cand.channels else ""
        factor = cfg.certain_pass_factor_by_channel.get(main, cfg.certain_pass_factor)
        paths: list[str] = []
        if cand.always_show_author:
            paths.append("always-show author")
        if threshold > 0 and has_verified_follow_author(cand, ctx) and topic >= threshold:
            paths.append(f"verified followed author and topic score {topic:g} >= {threshold:g}")
        if threshold > 0 and topic >= factor * threshold:
            paths.append(f"topic score {topic:g} >= {factor:g} x {threshold:g}")
        if threshold > 0 and seed_citations_of(cand) > 0 and topic >= threshold:
            paths.append(f"cites a seed and topic score {topic:g} >= {threshold:g}")
        if not paths:
            return Verdict(
                VerdictKind.NEUTRAL,
                f"no certain-pass path (topic {topic:g}, threshold {threshold:g})",
            )
        reason = "; ".join(paths) + f"; {venue_note}"
        cand.certain_pass = True
        cand.certain_pass_reason = reason
        return Verdict(VerdictKind.PASS, reason)


CORE_DROP_RULES: tuple[type[BaseRule], ...] = (
    SeenRule,
    ParatextRule,
    RepositoryVenueRule,
    PredatoryVenueRule,
    VenueQualityRule,
    PriorUserDropRule,
)


def default_rules(profile: Profile, *, extra_rules: Sequence[Rule] = ()) -> list[Rule]:
    """The full chain: core drop rules, profile YAML rules, ``extra_rules``, certain pass."""
    chain: list[Rule] = [cls() for cls in CORE_DROP_RULES]
    chain.extend(rules_from_profile(profile))
    chain.extend(extra_rules)
    chain.append(CertainPassRule())
    return chain
