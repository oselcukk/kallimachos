"""Weight learning from feedback.

delta = learning_rate * (pos - neg) / (evidence + 1); mult_new = clamp(mult_old * (1 + delta)).
``propose`` mode writes nothing; ``auto`` writes directly. Every change goes to ``history``.

Units: channel multiplier, followed-author bonus, venue multiplier, phrase multiplier
(``<channel>/<term>``). Evidence per candidate is the *effective* (last) tag:
``pos = good + read + archive + ask``, ``neg = irrelevant``. Which units a candidate touched is
read from the ``score`` entries of its ``rule_trace`` in ``runs/<id>.json``
(``scoring.candidate.score_trace``), so learning needs no re-scoring.

The author unit is a bonus, not a multiplier; it is learned as a multiplier on the base
follow-author weight (``follow_author_weight``, default 5) and stored as the
difference, so the same clamp applies:
``bonus_new = clamp((base + bonus_old) * (1 + delta)) - base``.
"""

from __future__ import annotations

import copy
import datetime as _dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from kallimachos.config.schema import WeightLearningConfig
from kallimachos.i18n import t
from kallimachos.interfaces import (
    BonusWeight,
    FeedbackEntry,
    FeedbackTag,
    MultWeight,
    RunResult,
    WeightChange,
    Weights,
    build,
    dump,
    json_schema,
)
from kallimachos.paths import DataLayout
from kallimachos.scoring.candidate import (
    TRACE_AUTHOR,
    TRACE_BROAD,
    TRACE_CHANNEL,
    TRACE_CORE,
    TRACE_VENUE,
    trace_values,
)
from kallimachos.state import store

UnitKind = Literal["channel", "author", "venue", "phrase"]

POSITIVE_TAGS: frozenset[FeedbackTag] = frozenset(
    {FeedbackTag.GOOD, FeedbackTag.READ, FeedbackTag.ARCHIVE, FeedbackTag.ASK}
)
NEGATIVE_TAGS: frozenset[FeedbackTag] = frozenset({FeedbackTag.IRRELEVANT})
DEFAULT_FOLLOW_AUTHOR_WEIGHT = 5.0

__all__ = [
    "WeightProposal",
    "apply",
    "commit",
    "count_evidence",
    "propose",
    "render_report",
    "save_weights",
    "update_multiplier",
]


@dataclass
class WeightProposal:
    unit_kind: UnitKind
    unit_id: str
    old: float
    new: float
    pos: int
    neg: int

    @property
    def evidence(self) -> int:
        return self.pos + self.neg


def clamp(value: float, cfg: WeightLearningConfig) -> float:
    lo, hi = cfg.clamp
    return max(lo, min(hi, value))


def update_multiplier(old: float, pos: int, neg: int, cfg: WeightLearningConfig) -> float | None:
    """Apply the weight-update formula; ``None`` when evidence < ``min_evidence``."""
    evidence = pos + neg
    if evidence < cfg.min_evidence:
        return None
    delta = cfg.learning_rate * (pos - neg) / (evidence + 1)
    return round(clamp(old * (1 + delta), cfg), 6)


def _effective_votes(feedback: Sequence[FeedbackEntry]) -> dict[tuple[str, str], FeedbackTag]:
    """(run_id, candidate_id) -> last tag (``irrelevant`` then ``good``: good wins)."""
    votes: dict[tuple[str, str], FeedbackTag] = {}
    for entry in sorted(feedback, key=lambda e: e.at):
        votes[(entry.run_id, entry.candidate_id)] = entry.tag
    return votes


def count_evidence(
    feedback: Sequence[FeedbackEntry], runs: Mapping[str, RunResult]
) -> dict[tuple[UnitKind, str], tuple[int, int]]:
    """``(unit kind, unit id) -> (pos, neg)`` from the effective tag of every candidate."""
    counts: dict[tuple[UnitKind, str], list[int]] = {}

    def bump(kind: UnitKind, unit_id: str, positive: bool) -> None:
        cell = counts.setdefault((kind, unit_id), [0, 0])
        cell[0 if positive else 1] += 1

    for (run_id, cid), tag in _effective_votes(feedback).items():
        run = runs.get(run_id)
        if run is None:
            continue
        cand = next((c for c in run.candidates if c.id == cid), None)
        if cand is None:
            continue
        if tag in POSITIVE_TAGS:
            positive = True
        elif tag in NEGATIVE_TAGS:
            positive = False
        else:
            continue
        channels = trace_values(cand, TRACE_CHANNEL) or list(cand.channels)
        for ch in dict.fromkeys(channels):
            bump("channel", ch, positive)
        phrases = trace_values(cand, TRACE_CORE) + trace_values(cand, TRACE_BROAD)
        for phrase in dict.fromkeys(phrases):
            bump("phrase", phrase, positive)
        for key in dict.fromkeys(trace_values(cand, TRACE_AUTHOR)):
            bump("author", key, positive)
        for key in dict.fromkeys(trace_values(cand, TRACE_VENUE)):
            bump("venue", key, positive)
    return {k: (v[0], v[1]) for k, v in counts.items()}


def _current(weights: Weights, kind: UnitKind, unit_id: str, base: float) -> float:
    if kind == "channel":
        return weights.channel.get(unit_id, MultWeight()).mult
    if kind == "venue":
        return weights.venue.get(unit_id, MultWeight()).mult
    if kind == "phrase":
        return weights.phrase.get(unit_id, MultWeight()).mult
    return (base + weights.author.get(unit_id, BonusWeight()).bonus) / base if base else 1.0


def propose(
    feedback: Sequence[FeedbackEntry],
    runs: Mapping[str, RunResult],
    weights: Weights,
    cfg: WeightLearningConfig,
    *,
    follow_author_weight: float = DEFAULT_FOLLOW_AUTHOR_WEIGHT,
) -> list[WeightProposal]:
    """Count evidence per unit from feedback and the candidates' rule traces."""
    out: list[WeightProposal] = []
    for (kind, unit_id), (pos, neg) in sorted(count_evidence(feedback, runs).items()):
        old_mult = _current(weights, kind, unit_id, follow_author_weight)
        new_mult = update_multiplier(old_mult, pos, neg, cfg)
        if new_mult is None:
            continue
        if kind == "author":
            old = round(old_mult * follow_author_weight - follow_author_weight, 6)
            new = round(new_mult * follow_author_weight - follow_author_weight, 6)
        else:
            old, new = round(old_mult, 6), new_mult
        if new == old:
            continue
        out.append(WeightProposal(kind, unit_id, old, new, pos, neg))
    return out


def apply(weights: Weights, proposals: Sequence[WeightProposal], *, at: _dt.datetime) -> Weights:
    """Return a new ``Weights`` with proposals applied and history appended."""
    new = copy.deepcopy(weights)
    for p in proposals:
        if p.unit_kind == "channel":
            new.channel[p.unit_id] = MultWeight(p.new)
        elif p.unit_kind == "venue":
            new.venue[p.unit_id] = MultWeight(p.new)
        elif p.unit_kind == "phrase":
            new.phrase[p.unit_id] = MultWeight(p.new)
        else:
            new.author[p.unit_id] = BonusWeight(p.new)
        new.history.append(
            WeightChange(
                at=at,
                change=f"{p.unit_kind} {p.unit_id}: {p.old:g} -> {p.new:g}",
                evidence_counts={"pos": p.pos, "neg": p.neg, "evidence": p.evidence},
            )
        )
    if proposals:
        new.updated_at = at
    return new


def load_weights(layout: DataLayout) -> Weights:
    """``weights.json`` as :class:`Weights`; a missing file is the neutral default."""
    data = store.read_json(layout.weights_file, None)
    return Weights() if data is None else build(Weights, data, path=layout.weights_file.name)


def save_weights(layout: DataLayout, weights: Weights) -> None:
    """Write ``weights.json`` atomically with schema validation (through ``state.store``)."""
    store.write_json_atomic(layout.weights_file, dump(weights), schema=json_schema(Weights))


def commit(
    layout: DataLayout,
    weights: Weights,
    proposals: Sequence[WeightProposal],
    cfg: WeightLearningConfig,
    *,
    at: _dt.datetime,
    confirm: bool = False,
) -> Weights | None:
    """Write when ``cfg.mode == "auto"`` or the user confirmed; otherwise write nothing and
    return ``None`` (``propose`` mode)."""
    if not proposals:
        return None
    if cfg.mode != "auto" and not confirm:
        return None
    new = apply(weights, proposals, at=at)
    save_weights(layout, new)
    return new


def render_report(proposals: Sequence[WeightProposal], *, lang: str) -> str:
    """Markdown report shown to the user before ``--confirm``.

    Labels come from the ``weights.report.*`` i18n keys.
    """
    lines = [f"## {t('weights.report.title', lang)}", ""]
    if not proposals:
        lines.append(t("weights.report.none", lang))
        return "\n".join(lines) + "\n"
    lines.append(t("weights.report.columns", lang))
    lines.append("|---|---|---:|---:|---:|---:|")
    for p in proposals:
        lines.append(f"| {p.unit_kind} | {p.unit_id} | {p.old:g} | {p.new:g} | {p.pos} | {p.neg} |")
    lines.extend(["", t("weights.report.hint", lang)])
    return "\n".join(lines) + "\n"
