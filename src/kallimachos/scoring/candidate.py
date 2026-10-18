"""Build a :class:`Candidate` from a record and its :class:`ChannelScores`.

The run command calls :func:`build_candidate` for every record that passed scoring,
then runs the rule chain. Scoring evidence that weight learning needs later (matched
core/broad phrases, followed authors, venue keys) is written into ``rule_trace`` as neutral
``score`` entries so that ``feedback.weights.propose`` can read it back from ``runs/<id>.json``
without re-scoring.
"""

from __future__ import annotations

from collections.abc import Sequence

from kallimachos.interfaces import Candidate, Record, RuleTraceEntry, VerdictKind, WorkKind
from kallimachos.profile.schema import Profile
from kallimachos.scoring.field import classify
from kallimachos.scoring.topic import ChannelScores

TRACE_RULE_SCORE = "score"
TRACE_CHANNEL = "channel:"
TRACE_CORE = "core:"
TRACE_BROAD = "broad:"
TRACE_AUTHOR = "follow_author:"
TRACE_VENUE = "venue:"
TRACE_TOPIC = "topic_score:"
TRACE_SEED = "seed_citations:"
TRACE_VERIFIED = "verified_follow_author"

MAX_AUTHORS = 15


def authors_short(names: Sequence[str], *, max_names: int = 3, etal: str = "et al.") -> str:
    shown = [n for n in names[:max_names] if n]
    text = ", ".join(shown)
    if len(names) > max_names:
        text += f" {etal}"
    return text


def score_trace(scores: ChannelScores) -> list[RuleTraceEntry]:
    """Neutral trace entries carrying the scoring evidence (read back by weight learning)."""
    out: list[RuleTraceEntry] = []

    def add(reason: str) -> None:
        out.append(
            RuleTraceEntry(rule=TRACE_RULE_SCORE, verdict=VerdictKind.NEUTRAL, reason=reason)
        )

    if scores.main_channel is not None:
        add(f"{TRACE_TOPIC}{scores.topic_score:g}")
    for ch_id in scores.channels:
        add(f"{TRACE_CHANNEL}{ch_id}")
        cs = scores.by_channel.get(ch_id)
        if cs is None:
            continue
        for term in cs.matched_core:
            add(f"{TRACE_CORE}{ch_id}/{term}")
        for term in cs.matched_broad:
            add(f"{TRACE_BROAD}{ch_id}/{term}")
    for key in scores.follow_authors:
        add(f"{TRACE_AUTHOR}{key}")
    if scores.verified_follow_author:
        add(TRACE_VERIFIED)
    for key in scores.venue_keys:
        add(f"{TRACE_VENUE}{key}")
    if scores.seed_citations:
        add(f"{TRACE_SEED}{scores.seed_citations}")
    return out


def trace_values(cand: Candidate, prefix: str) -> list[str]:
    """Values of ``score`` trace entries with ``prefix`` (``core:A/term`` -> ``A/term``)."""
    return [
        e.reason[len(prefix) :]
        for e in cand.rule_trace
        if e.rule == TRACE_RULE_SCORE and e.reason.startswith(prefix)
    ]


def topic_score_of(cand: Candidate) -> float | None:
    values = trace_values(cand, TRACE_TOPIC)
    if not values:
        return None
    try:
        return float(values[-1])
    except ValueError:
        return None


def build_candidate(
    record: Record,
    scores: ChannelScores,
    profile: Profile,
    *,
    candidate_id: str,
    keys: Sequence[str],
    no: int = 0,
) -> Candidate:
    """Candidate from a record and its scores; ``no`` is set by ``rank``."""
    names = [a.name for a in record.authors][:MAX_AUTHORS]
    channel_names = [
        c.name for ch in scores.channels if (c := profile.channel_by_id(ch)) is not None
    ]
    kind = record.kind
    if kind is WorkKind.UNKNOWN and record.arxiv_id and not record.doi:
        kind = WorkKind.PREPRINT
    cand = Candidate(
        no=no,
        id=candidate_id,
        title=record.title,
        authors=names,
        authors_short=authors_short(names),
        venue=record.venue,
        venue_short=record.venue,
        date=record.published or record.created,
        url=record.url,
        doi=record.doi,
        arxiv=record.arxiv_id,
        abstract=record.abstract,
        abstract_available=bool(record.abstract and record.abstract.strip()),
        channels=list(scores.channels),
        channel_names=channel_names,
        score=scores.total,
        threshold=scores.threshold,
        score_reasons=list(scores.reasons),
        always_show_author=scores.always_show,
        full_journal=scores.full_journal,
        noise=scores.noise,
        sources=sorted(record.sources),
        field_class=classify(record),
        keys=list(keys),
        kind=kind,
    )
    cand.rule_trace.extend(score_trace(scores))
    return cand
