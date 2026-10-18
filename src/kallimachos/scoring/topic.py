"""Topic score per channel.

The archive classifier reuses :func:`score` (no second classifier).

Phrase matching (rule 1): title and abstract are reduced with ``records.normalize.match_key``
and padded with spaces, so a phrase matches only as a whole, space-bounded token sequence. A
phrase that is part of a longer matched phrase is not counted again. A phrase containing a
single-letter token (``a loop``) is additionally verified case-sensitively against the raw text.

Learned weights enter here and only here: ``weights.channel[id].mult`` scales the
channel score, ``weights.phrase["<channel>/<term>"].mult`` scales that phrase's contribution,
``weights.venue[<issn>].mult`` scales the topic score of a record published in that venue, and
``weights.author[<key>].bonus`` is added to the follow-author bonus.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from kallimachos.config.schema import ScoringConfig, ScoringWeights
from kallimachos.interfaces import Record, RunKind, Weights
from kallimachos.profile.schema import (
    AlwaysShowAuthor,
    Channel,
    FollowAuthor,
    Journal,
    Profile,
)
from kallimachos.records.normalize import match_key

__all__ = [
    "ChannelScore",
    "ChannelScores",
    "author_matches",
    "author_weight_key",
    "journal_for",
    "matched_phrases",
    "noise_terms_hit",
    "phrase_count",
    "score",
    "venue_weight_keys",
]


@dataclass
class ChannelScore:
    channel_id: str
    core: float = 0.0
    broad: float = 0.0
    kw: float = 0.0
    category_bonus: float = 0.0
    penalty: float = 0.0
    noise: bool = False
    cancelled: bool = False  # exclude phrase matched without any core phrase
    raw: float = 0.0
    final: float = 0.0  # raw * priority_multiplier * learned channel multiplier
    threshold: float = 0.0
    matched_core: list[str] = field(default_factory=list)
    matched_broad: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


@dataclass
class ChannelScores:
    by_channel: dict[str, ChannelScore] = field(default_factory=dict)
    main_channel: str | None = None
    bonuses: dict[str, float] = field(
        default_factory=dict
    )  # always_show, follow_author, seed_citation ...
    mandatory: bool = False  # always_show author or full journal
    total: float = 0.0
    passes: bool = False
    reasons: list[str] = field(default_factory=list)
    # Detail kept for Candidate building and weight learning (beyond the spec's minimum shape).
    channels: list[str] = field(default_factory=list)  # main first, then bonus channels
    always_show: bool = False
    full_journal: bool = False
    topic_filtered_journal: bool = False
    noise: bool = False
    noise_terms: list[str] = field(default_factory=list)
    follow_authors: list[str] = field(default_factory=list)  # weight keys of matched authors
    verified_follow_author: bool = False
    seed_citations: int = 0
    foundational_citations: int = 0
    venue_keys: list[str] = field(default_factory=list)
    cancelled_channels: list[str] = field(default_factory=list)

    @property
    def topic_score(self) -> float:
        """Pure main-channel score without bonuses (what certain-pass compares)."""
        if self.main_channel is None:
            return 0.0
        return self.by_channel[self.main_channel].final

    @property
    def threshold(self) -> float:
        if self.main_channel is None:
            return 0.0
        return self.by_channel[self.main_channel].threshold


# ---------------------------------------------------------------------------
# phrase matching
# ---------------------------------------------------------------------------


def _padded(text: str) -> str:
    return f" {match_key(text)} "


def _has_single_letter_token(phrase_key: str) -> bool:
    return any(len(tok) == 1 for tok in phrase_key.split())


def _case_sensitive_present(raw_text: str, phrase: str) -> bool:
    """Case-sensitive, letter-bounded search for phrases with single-letter tokens."""
    tokens = [t for t in re.split(r"[-\s]+", phrase.strip()) if t]
    if not tokens:
        return False
    pattern = r"[-\s]+".join(re.escape(t) for t in tokens)
    return re.search(rf"(?<![A-Za-z]){pattern}(?![A-Za-z])", raw_text) is not None


def phrase_count(text: str, phrase: str) -> int:
    """Whole-phrase, space-bounded count; a phrase inside a longer matched phrase does not count;
    phrases containing a single-letter token are case-sensitive.

    Nesting is a property of a phrase *list*, so this function counts occurrences of one phrase;
    :func:`matched_phrases` applies the nesting rule across a list.
    """
    key = match_key(phrase)
    if not key:
        return 0
    if _has_single_letter_token(key) and not _case_sensitive_present(text, phrase):
        return 0
    return _padded(text).count(f" {key} ")


def matched_phrases(text: str, phrases: Iterable[str]) -> list[str]:
    """Phrases (original spelling) that occur in ``text``; a matched phrase whose tokens are a
    sub-sequence of another matched phrase is dropped (rule 1)."""
    hits: list[tuple[str, str]] = []
    seen_keys: set[str] = set()
    for phrase in phrases:
        key = match_key(phrase)
        if not key or key in seen_keys:
            continue
        if phrase_count(text, phrase) > 0:
            hits.append((key, phrase))
            seen_keys.add(key)
    out: list[str] = []
    for key, phrase in hits:
        nested = any(other != key and f" {key} " in f" {other} " for other, _ in hits)
        if not nested:
            out.append(phrase)
    return out


def noise_terms_hit(
    title: str, abstract: str, terms: Sequence[str], abstract_min: int
) -> list[str]:
    """Noise terms when the title has at least one or the abstract at least ``abstract_min``
    distinct terms."""
    if not terms:
        return []
    in_title = matched_phrases(title, terms)
    in_abstract = matched_phrases(abstract, terms)
    if in_title or len(in_abstract) >= abstract_min:
        out: list[str] = []
        for term in in_title + in_abstract:
            if term not in out:
                out.append(term)
        return out
    return []


# ---------------------------------------------------------------------------
# authors and journals
# ---------------------------------------------------------------------------


def _name_parts(name: str) -> tuple[str, str] | None:
    tokens = match_key(name).split()
    if not tokens:
        return None
    return tokens[0][0], tokens[-1]


def author_matches(
    record_name: str,
    *,
    name: str,
    aliases: Sequence[str] = (),
    record_orcid: str | None = None,
    record_openalex_id: str | None = None,
    orcid: str | None = None,
    openalex_id: str | None = None,
) -> str | None:
    """``"id"`` for an identifier match, ``"name"`` for first-initial + surname match (also
    against aliases), else ``None``."""
    if orcid and record_orcid and orcid.lower() == record_orcid.lower():
        return "id"
    if openalex_id and record_openalex_id and openalex_id.lower() == record_openalex_id.lower():
        return "id"
    rec = _name_parts(record_name)
    if rec is None:
        return None
    for candidate_name in (name, *aliases):
        parts = _name_parts(candidate_name)
        if parts is not None and parts == rec:
            return "name"
    return None


def author_weight_key(author: FollowAuthor | AlwaysShowAuthor) -> str:
    """Key of ``weights.author``: ORCID, else OpenAlex id, else the normalised name."""
    return author.orcid or author.openalex_id or match_key(author.name)


def journal_for(record: Record, profile: Profile) -> tuple[Journal, Channel] | None:
    """First profile journal whose ISSN, or OpenAlex source id (``Record.venue_openalex_id``),
    matches the record."""
    issns = {i.lower() for i in record.issn}
    sid = (record.venue_openalex_id or "").rsplit("/", 1)[-1].upper() or None
    if not issns and not sid:
        return None
    for channel in profile.channels:
        for journal in channel.journals:
            if any(i.lower() in issns for i in journal.issn):
                return journal, channel
            jid = (journal.openalex_source_id or "").rsplit("/", 1)[-1].upper()
            if sid and jid and jid == sid:
                return journal, channel
    return None


def venue_weight_keys(record: Record) -> list[str]:
    """Keys under which ``weights.venue`` may hold a multiplier for this record."""
    return [i.lower() for i in record.issn]


# ---------------------------------------------------------------------------
# per-channel computation
# ---------------------------------------------------------------------------


def _phrase_mult(weights: Weights, channel_id: str, term: str) -> float:
    entry = weights.phrase.get(f"{channel_id}/{term}")
    return entry.mult if entry is not None else 1.0


def _phrase_points(
    phrases: Sequence[str], unit: float, weights: Weights, channel_id: str, cap: int = 2
) -> float:
    """``unit`` per matched phrase scaled by the learned phrase multiplier, best ``cap`` kept."""
    contributions = sorted(
        (unit * _phrase_mult(weights, channel_id, p) for p in phrases), reverse=True
    )
    return float(sum(contributions[:cap]))


def _record_categories(record: Record) -> set[str]:
    cats = set(record.categories)
    if record.primary_category:
        cats.add(record.primary_category)
    return cats


def _category_bonus(
    record: Record, channel: Channel, w: ScoringWeights, run_kind: RunKind
) -> tuple[float, list[str]]:
    reasons: list[str] = []
    bonus = 0.0
    cats = _record_categories(record)
    if cats & set(channel.arxiv.primary):
        bonus += w.arxiv_primary_category
        reasons.append("arxiv category primary")
    elif cats & set(channel.arxiv.secondary):
        bonus += w.arxiv_secondary_category
        reasons.append("arxiv category secondary")
    if run_kind is RunKind.WEEKLY and record.msc:
        primary = set(channel.msc.primary)
        secondary = set(channel.msc.secondary)
        if record.msc[0] in primary:
            bonus += w.msc_primary_main
            reasons.append("msc primary main")
        elif set(record.msc) & primary:
            bonus += w.msc_primary
            reasons.append("msc primary")
        elif set(record.msc) & secondary:
            bonus += w.msc_secondary
            reasons.append("msc secondary")
    return bonus, reasons


def _arxiv_category_mismatch(record: Record, channel: Channel) -> bool:
    if "arxiv" not in record.sources:
        return False
    cats = _record_categories(record)
    if not cats:
        return False
    return not (cats & (set(channel.arxiv.primary) | set(channel.arxiv.secondary)))


def _score_channel(
    record: Record,
    channel: Channel,
    title: str,
    abstract: str,
    weights: Weights,
    cfg: ScoringConfig,
    run_kind: RunKind,
) -> ChannelScore:
    w = cfg.weights
    cs = ChannelScore(channel_id=channel.id)
    cs.threshold = float(cfg.threshold.get(channel.priority, max(cfg.threshold.values())))
    tc = matched_phrases(title, channel.keywords.core)
    ac = matched_phrases(abstract, channel.keywords.core)
    tb = matched_phrases(title, channel.keywords.broad)
    ab = matched_phrases(abstract, channel.keywords.broad)
    cs.matched_core = list(dict.fromkeys(tc + ac))
    cs.matched_broad = list(dict.fromkeys(tb + ab))
    cs.core = _phrase_points(tc, w.title_core, weights, channel.id) + _phrase_points(
        ac, w.abstract_core, weights, channel.id
    )
    cs.broad = _phrase_points(tb, w.title_broad, weights, channel.id) + _phrase_points(
        ab, w.abstract_broad, weights, channel.id
    )
    if cs.core == 0 and not tb and len(ab) < 2:
        cs.broad = 0.0  # broad only: a title hit or at least two abstract hits are required
    if cs.core == 0 and cs.broad == 0:
        return cs
    text = f"{title} . {abstract}"
    excluded = matched_phrases(text, channel.negative.exclude)
    if excluded and cs.core == 0:
        cs.cancelled = True
        cs.reasons.append(f"excluded: {excluded[0]}")
        return cs
    cs.kw = min(cs.core + cs.broad, w.channel_cap)
    cs.category_bonus, cat_reasons = _category_bonus(record, channel, w, run_kind)
    mismatch = _arxiv_category_mismatch(record, channel)
    if mismatch:
        cs.kw *= w.arxiv_category_mismatch_factor
    penalties = matched_phrases(text, channel.negative.penalty)
    cs.penalty = max(w.penalty * len(penalties), w.penalty_floor)
    cs.raw = cs.kw + cs.category_bonus + cs.penalty
    noise = noise_terms_hit(title, abstract, channel.noise_terms, cfg.noise_abstract_min_terms)
    if noise:
        cs.noise = True
        cs.raw *= cfg.noise_factor
    priority_mult = cfg.priority_multiplier.get(channel.priority, 1.0)
    learned = weights.channel.get(channel.id)
    channel_mult = learned.mult if learned is not None else 1.0
    cs.final = round(cs.raw * priority_mult * channel_mult, 4)
    if tc:
        cs.reasons.append("title core: " + ", ".join(tc[:3]))
    if ac:
        cs.reasons.append("abstract core: " + ", ".join(ac[:3]))
    if cs.broad and tb:
        cs.reasons.append("title broad: " + ", ".join(tb[:3]))
    if cs.broad and ab:
        cs.reasons.append("abstract broad: " + ", ".join(ab[:3]))
    cs.reasons.extend(cat_reasons)
    if mismatch:
        cs.reasons.append("arxiv category outside channel (reduced)")
    if penalties:
        cs.reasons.append("penalty: " + ", ".join(penalties[:2]))
    if noise:
        cs.reasons.append("noise: " + ", ".join(noise[:3]))
    if channel_mult != 1.0:
        cs.reasons.append(f"learned channel multiplier {channel_mult:g}")
    return cs


# ---------------------------------------------------------------------------
# bonuses, pass decision
# ---------------------------------------------------------------------------


def _citation_bonus(n: int, base: float, extra: float, cap: float) -> float:
    if n <= 0:
        return 0.0
    return min(base + extra * (n - 1), cap)


def _cited_seed_channels(record: Record, profile: Profile) -> tuple[int, list[str], int]:
    """(cited seed count, channels of the cited seeds, cited foundational count) from
    ``referenced_works`` against profile OpenAlex ids."""
    refs = set(record.referenced_works)
    if not refs:
        return 0, [], 0
    seeds = [s for s in profile.seeds if s.openalex_id and s.openalex_id in refs]
    channels: list[str] = []
    for seed in seeds:
        for ch in seed.channels:
            if ch not in channels:
                channels.append(ch)
    foundational = sum(1 for f in profile.foundational if f.openalex_id in refs)
    return len(seeds), channels, foundational


def _match_authors(
    record: Record, profile: Profile, weights: Weights, w: ScoringWeights
) -> tuple[list[str], list[str], float, bool, list[str]]:
    """(always-show names, follow weight keys, follow bonus, verified follow, extra channels)."""
    always: list[str] = []
    follow_keys: list[str] = []
    follow_bonus = 0.0
    verified = False
    extra_channels: list[str] = []
    for author in record.authors:
        for a in profile.authors.always_show:
            how = author_matches(
                author.name,
                name=a.name,
                aliases=a.aliases,
                record_orcid=author.orcid,
                record_openalex_id=author.openalex_id,
                orcid=a.orcid,
                openalex_id=a.openalex_id,
            )
            if how and a.name not in always:
                always.append(a.name)
        for f in profile.authors.follow:
            how = author_matches(
                author.name,
                name=f.name,
                record_orcid=author.orcid,
                record_openalex_id=author.openalex_id,
                orcid=f.orcid,
                openalex_id=f.openalex_id,
            )
            if how is None:
                continue
            key = author_weight_key(f)
            if key in follow_keys:
                continue
            follow_keys.append(key)
            learned = weights.author.get(key)
            follow_bonus += w.follow_author + (learned.bonus if learned is not None else 0.0)
            extra_channels.extend(f.channels)
            if f.status == "verified":
                verified = True
    return always, follow_keys, follow_bonus, verified, extra_channels


def score(
    record: Record,
    profile: Profile,
    weights: Weights,
    cfg: ScoringConfig,
    *,
    run_kind: RunKind = RunKind.DAILY,
    seed_citations: int = 0,
    foundational_citations: int = 0,
) -> ChannelScores:
    """Full topic-score computation including bonuses and the pass decision.

    ``seed_citations`` / ``foundational_citations`` are counts the caller already knows (for
    example from a citation query); citations found in ``record.referenced_works`` against the
    profile's OpenAlex ids are used when they are larger.
    """
    w = cfg.weights
    out = ChannelScores()
    title = record.title or ""
    abstract = record.abstract or ""
    if not abstract.strip() and record.keywords:
        abstract = " . ".join(record.keywords)  # keyword list stands in for a missing abstract

    for channel in profile.channels:
        cs = _score_channel(record, channel, title, abstract, weights, cfg, run_kind)
        if cs.cancelled:
            out.cancelled_channels.append(channel.id)
            out.reasons.extend(f"{channel.id}: {r}" for r in cs.reasons)
            continue
        if cs.final <= 0:
            continue
        out.by_channel[channel.id] = cs
        for term in noise_terms_hit(
            title, abstract, channel.noise_terms, cfg.noise_abstract_min_terms
        ):
            if term not in out.noise_terms:
                out.noise_terms.append(term)

    # learned venue multiplier applies to the topic score of every channel
    out.venue_keys = venue_weight_keys(record)
    venue_mult = 1.0
    for key in out.venue_keys:
        learned = weights.venue.get(key)
        if learned is not None:
            venue_mult = learned.mult
            break
    if venue_mult != 1.0:
        for cs in out.by_channel.values():
            cs.final = round(cs.final * venue_mult, 4)
            cs.reasons.append(f"learned venue multiplier {venue_mult:g}")

    ranked = sorted(out.by_channel, key=lambda k: -out.by_channel[k].final)
    out.channels = list(ranked)
    out.main_channel = ranked[0] if ranked else None
    out.noise = out.main_channel is not None and out.by_channel[out.main_channel].noise

    bonus_total = 0.0
    always, follow_keys, follow_bonus, verified_follow, extra_channels = _match_authors(
        record, profile, weights, w
    )
    if always:
        out.always_show = True
        out.mandatory = True
        out.bonuses["always_show"] = w.always_show_author
        bonus_total += w.always_show_author
        out.reasons.append("always-show author: " + ", ".join(always))
    if follow_keys:
        out.follow_authors = follow_keys
        out.verified_follow_author = verified_follow
        out.bonuses["follow_author"] = follow_bonus
        bonus_total += follow_bonus
        out.reasons.append("followed author")

    n_seed_ref, seed_channels, n_found_ref = _cited_seed_channels(record, profile)
    n_seed = max(seed_citations, n_seed_ref)
    n_found = max(foundational_citations, n_found_ref)
    out.seed_citations = n_seed
    out.foundational_citations = n_found
    if n_seed:
        b = _citation_bonus(n_seed, w.seed_citation, w.seed_citation_extra, w.seed_citation_cap)
        out.bonuses["seed_citation"] = b
        bonus_total += b
        out.reasons.append(f"cites {n_seed} seed work(s)")
        extra_channels.extend(seed_channels)
    if n_found:
        b = _citation_bonus(
            n_found,
            w.foundational_citation,
            w.foundational_citation_extra,
            w.foundational_citation_cap,
        )
        out.bonuses["foundational_citation"] = b
        bonus_total += b
        out.reasons.append(f"cites {n_found} foundational work(s)")

    jc = journal_for(record, profile)
    if jc is not None:
        journal, channel = jc
        if journal.mode == "full":
            out.full_journal = True
            out.mandatory = True
            out.bonuses["full_journal"] = w.full_journal
            bonus_total += w.full_journal
            out.reasons.append(f"fully followed journal: {journal.name}")
            extra_channels.append(channel.id)
        else:
            out.topic_filtered_journal = True
            out.reasons.append(f"topic-filtered journal: {journal.name}")

    for ch in extra_channels:
        if profile.channel_by_id(ch) is not None and ch not in out.channels:
            out.channels.append(ch)

    topic = out.topic_score
    out.total = round(topic + bonus_total, 4)
    passes_topic = out.main_channel is not None and topic >= out.threshold
    citation_passes = n_seed > 0
    if run_kind is RunKind.WEEKLY and citation_passes:
        flag_channels = set(seed_channels)
        if out.main_channel:
            flag_channels.add(out.main_channel)
        citation_passes = any(
            (c := profile.channel_by_id(ch)) is not None and c.citation_alone_passes
            for ch in flag_channels
        )
    out.passes = out.mandatory or passes_topic or bool(follow_keys) or citation_passes
    for ch_id in ranked:
        cs = out.by_channel[ch_id]
        out.reasons.append(f"{ch_id} ({cs.final:g}): " + "; ".join(cs.reasons))
    return out
