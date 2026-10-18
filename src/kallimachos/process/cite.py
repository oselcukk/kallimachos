"""Reference -> catalog matching into the citation graph (``process cite``).

Priority DOI -> arXiv -> title (>= 4 words, year +-1, first surname, token_set_ratio >= 88,
partial_ratio >= 90) -> author-year-title. Certain >= 88; uncertain 75-88 or close runner-up or
short title. Self-citations dropped.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from rapidfuzz import fuzz

from kallimachos.archive.catalog import Catalog
from kallimachos.interfaces import Card, CardReference, CatalogEntry, GraphEdge
from kallimachos.records.normalize import match_key, strip_accents

CERTAIN_MIN = 88
UNCERTAIN_MIN = 75
RUNNER_UP_GAP = 5
TITLE_MIN_WORDS = 4
TITLE_PARTIAL_MIN = 90
SURNAME_PARTIAL_MIN = 85

Method = Literal["doi", "arxiv", "title", "author_year_title"]

_DOI_RE = re.compile(r"10\.[0-9]{4,9}/[^\s\"'<>]+", re.IGNORECASE)
_ARXIV_NEW_RE = re.compile(r"(?<![0-9])([0-9]{4}\.[0-9]{4,5})(v[0-9]+)?\b", re.IGNORECASE)
_ARXIV_OLD_RE = re.compile(r"\b([a-z\-]+(?:\.[A-Z]{2})?/[0-9]{7})(v[0-9]+)?\b")
_YEAR_RE = re.compile(r"\b(1[89][0-9]{2}|20[0-9]{2})\b")
_ROMAN_RE = re.compile(r"^(?=[ivxlc]+$)i{0,3}v?i{0,3}x{0,3}l?c{0,3}$")
_TRANSLIT = (
    ("tsch", "s"),
    ("sch", "s"),
    ("tch", "s"),
    ("sh", "s"),
    ("ch", "s"),
    ("ph", "f"),
    ("ck", "k"),
    ("ae", "a"),
    ("oe", "o"),
    ("ue", "u"),
)


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    m = _DOI_RE.search(value)
    if not m:
        return None
    return m.group(0).rstrip(".,;:)]}").lower()


def normalize_arxiv(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    text = re.sub(r"^(arxiv:|https?://arxiv\.org/(abs|pdf)/)", "", text, flags=re.IGNORECASE)
    m = _ARXIV_NEW_RE.search(text) or _ARXIV_OLD_RE.search(text)
    if not m:
        return None
    return m.group(1).lower()


def surname_key(name: str) -> str:
    """Transliteration-tolerant surname key: ``Tschebyscheff``/``Chebyshev`` -> ``sebisef``.

    Accents dropped, ``tsch/sch/tch/sh/ch -> s``, ``ph -> f``, ``y -> i``, ``w/v -> f``,
    ``ae/oe/ue -> a/o/u``, doubled letters collapsed. ``Surname, Given`` and ``Given Surname``
    both yield the surname.
    """
    text = name.strip()
    if "," in text:
        text = text.split(",", 1)[0]
    else:
        tokens = [t for t in re.split(r"\s+", text) if t]
        text = tokens[-1] if tokens else ""
    key = strip_accents(text).lower()
    key = re.sub(r"[^a-z]", "", key)
    for src, dst in _TRANSLIT:
        key = key.replace(src, dst)
    key = key.replace("y", "i").replace("w", "f").replace("v", "f")
    return re.sub(r"(.)\1+", r"\1", key)


def _years(text: str) -> set[int]:
    return {int(y) for y in _YEAR_RE.findall(text)}


def _numeral_tokens(key: str) -> set[str]:
    return {t for t in key.split() if t.isdigit() or _ROMAN_RE.match(t)}


@dataclass
class Target:
    """A work in the archive that a reference can point to."""

    eid: str
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    arxiv: str | None = None

    @property
    def title_key(self) -> str:
        return match_key(self.title)

    @property
    def first_surname(self) -> str:
        return surname_key(self.authors[0]) if self.authors else ""


def _target_from_entry(entry: CatalogEntry) -> Target | None:
    if not entry.sha256:
        return None
    return Target(
        eid=entry.sha256[:12],
        title=entry.title,
        authors=list(entry.authors),
        year=entry.year,
        doi=normalize_doi(entry.doi),
        arxiv=normalize_arxiv(entry.arxiv),
    )


def build_targets(entries: Iterable[CatalogEntry], cards: Iterable[Card]) -> dict[str, Target]:
    """Catalog entries by eid, completed (never overridden) from the cards' citations."""
    targets: dict[str, Target] = {}
    for entry in entries:
        t = _target_from_entry(entry)
        if t is not None:
            targets[t.eid] = t
    for card in cards:
        c = card.citation
        t = targets.setdefault(card.eid, Target(eid=card.eid))
        t.title = t.title or c.title
        t.authors = t.authors or list(c.authors)
        t.year = t.year if t.year is not None else (c.year_catalog or c.year)
        t.doi = t.doi or normalize_doi(c.doi)
        t.arxiv = t.arxiv or normalize_arxiv(c.arxiv)
    return targets


@dataclass
class _Candidate:
    target: Target
    score: float
    method: Method


def _year_ok(target: Target, ref_years: set[int]) -> bool:
    if target.year is None or not ref_years:
        return True
    return any(abs(target.year - y) <= 1 for y in ref_years)


def _surname_ok(target: Target, ref_text: str, ref_surnames: set[str]) -> bool:
    key = target.first_surname
    if not key:
        return True
    if key in ref_surnames:
        return True
    return fuzz.partial_ratio(key, " ".join(sorted(ref_surnames))) >= SURNAME_PARTIAL_MIN


def candidates_for(ref: CardReference, targets: Iterable[Target]) -> list[_Candidate]:
    """All plausible targets for ``ref`` with their score and method, best first."""
    text_doi = normalize_doi(ref.doi) or normalize_doi(ref.text)
    text_arxiv = normalize_arxiv(ref.arxiv) or normalize_arxiv(ref.text)
    ref_key = match_key(ref.text)
    ref_years = _years(ref.text)
    ref_surnames = {surname_key(tok) for tok in ref.text.replace(",", " ").split() if tok}
    ref_numerals = _numeral_tokens(ref_key)
    out: list[_Candidate] = []
    for t in targets:
        if text_doi and t.doi and text_doi == t.doi:
            out.append(_Candidate(t, 100.0, "doi"))
            continue
        if text_arxiv and t.arxiv and text_arxiv == t.arxiv:
            out.append(_Candidate(t, 100.0, "arxiv"))
            continue
        title_key = t.title_key
        if not title_key or not _year_ok(t, ref_years):
            continue
        tsr = float(fuzz.token_set_ratio(title_key, ref_key))
        if len(title_key.split()) >= TITLE_MIN_WORDS:
            partial = float(fuzz.partial_ratio(title_key, ref_key))
            if (
                tsr >= CERTAIN_MIN
                and partial >= TITLE_PARTIAL_MIN
                and _surname_ok(t, ref.text, ref_surnames)
                and _numeral_tokens(title_key) <= ref_numerals
            ):
                out.append(_Candidate(t, tsr, "title"))
                continue
        if t.first_surname and t.first_surname in ref_surnames and tsr >= UNCERTAIN_MIN:
            out.append(_Candidate(t, tsr, "author_year_title"))
    out.sort(key=lambda c: -c.score)
    return out


def classify(cands: Sequence[_Candidate]) -> tuple[_Candidate, bool] | None:
    """Best candidate and whether it is certain; None when nothing reaches ``UNCERTAIN_MIN``."""
    if not cands:
        return None
    best = cands[0]
    if best.score < UNCERTAIN_MIN:
        return None
    if best.method in ("doi", "arxiv"):
        return best, True
    runner_up = next((c for c in cands[1:] if c.target.eid != best.target.eid), None)
    close = runner_up is not None and best.score - runner_up.score < RUNNER_UP_GAP
    short = len(best.target.title_key.split()) < TITLE_MIN_WORDS
    certain = best.score >= CERTAIN_MIN and not close and not short
    return best, certain


def match_references(
    card: Card, catalog: Catalog, cards: Sequence[Card]
) -> tuple[list[GraphEdge], list[GraphEdge]]:
    """Return (certain edges, uncertain edges) for every reference of ``card``."""
    targets = build_targets(catalog.entries(), cards)
    return match_against(card, targets)


def match_against(
    card: Card, targets: dict[str, Target]
) -> tuple[list[GraphEdge], list[GraphEdge]]:
    certain: dict[str, GraphEdge] = {}
    uncertain: dict[str, GraphEdge] = {}
    others = [t for t in targets.values() if t.eid != card.eid]  # self-citations dropped
    for ref in card.references:
        decision = classify(candidates_for(ref, others))
        if decision is None:
            continue
        best, is_certain = decision
        edge = GraphEdge(
            source_eid=card.eid,
            target_eid=best.target.eid,
            key=ref.key,
            method=best.method,
            score=round(best.score, 1),
            text=ref.text[:500],
        )
        bucket = certain if is_certain else uncertain
        prev = bucket.get(edge.target_eid)
        if prev is None or edge.score > prev.score:
            bucket[edge.target_eid] = edge
    for eid in list(uncertain):
        if eid in certain:
            del uncertain[eid]
    return list(certain.values()), list(uncertain.values())
