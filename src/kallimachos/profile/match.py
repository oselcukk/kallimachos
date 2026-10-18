"""Match imported references to OpenAlex/Crossref/S2 records.

Order: DOI -> OpenAlex single record; else OpenAlex autocomplete (candidates with similarity
>= ``autocomplete_min_sim`` are opened as single records); else Crossref ``query.bibliographic``
-> DOI -> OpenAlex; else Semantic Scholar match (only when a key is configured).

The calls go through the shared :class:`~kallimachos.interfaces.HttpClient`; the small
:class:`Lookup` protocol isolates the request shapes so tests can pass a fake without HTTP.
Profile building never sends a key itself (the HTTP layer adds headers); the URLs below carry
only the bibliographic query.
"""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from kallimachos.config.schema import ProfileMatchConfig
from kallimachos.interfaces import Author, HttpClient, Record
from kallimachos.profile.importers import ImportedRef, clean_doi, first_author_surname
from kallimachos.records.normalize import match_key

Confidence = Literal["high", "medium", "low"]
Via = Literal[
    "doi", "openalex_autocomplete", "crossref_bibliographic", "s2_match", "orcid", "openalex_author"
]

OPENALEX_API = "https://api.openalex.org"
CROSSREF_API = "https://api.crossref.org"
S2_API = "https://api.semanticscholar.org/graph/v1"
ARXIV_DOI_PREFIX = "10.48550/arxiv."

_CONFIDENCE_RANK: dict[str, int] = {"high": 3, "medium": 2, "low": 1}


@dataclass
class WorkExtra:
    """OpenAlex fields that :class:`~kallimachos.interfaces.Record` has no slot for but profile
    proposals need."""

    source_id: str | None = None  # OpenAlex source id (S...)
    cited_by_count: int = 0
    topics: list[str] = field(default_factory=list)  # topic display names
    subfields: list[str] = field(default_factory=list)
    work_type: str = ""


@dataclass
class Match:
    ref: ImportedRef
    record: Record
    sim: float
    author_match: bool
    year_diff: int | None
    confidence: Confidence
    via: Via
    extra: WorkExtra = field(default_factory=WorkExtra)


@dataclass
class AutocompleteHit:
    id: str
    display_name: str
    hint: str = ""


class Lookup(Protocol):
    """Request shapes used by matching; implemented by :class:`OpenAlexLookup` and test fakes."""

    def work_by_doi(self, doi: str) -> tuple[Record, WorkExtra] | None: ...

    def work_by_id(self, openalex_id: str) -> tuple[Record, WorkExtra] | None: ...

    def autocomplete(self, title: str) -> list[AutocompleteHit]: ...

    def crossref_doi(self, title: str, author: str | None, year: int | None) -> str | None: ...

    def s2_doi(self, title: str) -> str | None: ...

    def works_by_ids(self, ids: Sequence[str]) -> list[tuple[Record, WorkExtra]]: ...

    def works_by_author(self, author_id: str) -> list[tuple[Record, WorkExtra]]: ...

    @property
    def requests(self) -> list[str]: ...


# ---------------------------------------------------------------------------
# Similarity and confidence
# ---------------------------------------------------------------------------


def title_similarity(a: str, b: str) -> float:
    """Similarity in [0, 1] on ``match_key`` strings. When both keys are longer than 15
    characters and one contains the other the result is at least 0.9."""
    ka, kb = match_key(a), match_key(b)
    if not ka or not kb:
        return 0.0
    ratio = difflib.SequenceMatcher(None, ka, kb).ratio()
    if len(ka) > 15 and len(kb) > 15 and (ka in kb or kb in ka):
        ratio = max(ratio, 0.9)
    return round(ratio, 4)


def confidence_for(
    sim: float, author_match: bool, year_diff: int | None, cfg: ProfileMatchConfig
) -> Confidence | None:
    """Match confidence (high/medium/low or ``None`` for no match)."""
    if sim >= cfg.high_sim and author_match:
        return "high"
    medium = (
        sim >= cfg.medium_sim
        and (author_match or (year_diff is not None and year_diff <= cfg.medium_year_tolerance))
    ) or (
        sim >= cfg.medium_sim_strict
        and year_diff is not None
        and year_diff <= cfg.strict_year_tolerance
    )
    if medium:
        if year_diff is not None and year_diff > cfg.old_year_gap and sim < cfg.old_year_gap_sim:
            return "low"
        return "medium"
    if sim >= cfg.low_sim and author_match:
        return "low"
    return None


def author_matches(local_authors: Sequence[str], record_authors: Sequence[Author]) -> bool:
    """First local surname appears in a record author name (accent/case-insensitive)."""
    surname = match_key(first_author_surname(list(local_authors)))
    if not surname:
        return False
    for author in record_authors:
        tokens = match_key(author.name).split()
        if surname in tokens or (len(surname) > 3 and any(surname in t for t in tokens)):
            return True
    return False


def year_difference(local_year: int | None, record: Record) -> int | None:
    if local_year is None or record.published is None:
        return None
    return abs(local_year - record.published.year)


def evaluate(
    ref: ImportedRef, record: Record, extra: WorkExtra, via: Via, cfg: ProfileMatchConfig
) -> Match | None:
    sim = title_similarity(ref.title, record.title)
    author = author_matches(ref.authors, record.authors)
    dy = year_difference(ref.year, record)
    confidence = confidence_for(sim, author, dy, cfg)
    if confidence is None:
        return None
    return Match(ref, record, sim, author, dy, confidence, via, extra)


def _better(a: Match | None, b: Match | None) -> Match | None:
    if a is None:
        return b
    if b is None:
        return a
    ka = (_CONFIDENCE_RANK[a.confidence], a.sim)
    kb = (_CONFIDENCE_RANK[b.confidence], b.sim)
    return a if ka >= kb else b


def match_reference(
    ref: ImportedRef,
    http: HttpClient,
    cfg: ProfileMatchConfig,
    *,
    lookup: Lookup | None = None,
    s2_enabled: bool = False,
) -> Match | None:
    """DOI -> OpenAlex single; else autocomplete; else Crossref bibliographic; else S2 (key).

    Accepts ``medium`` or better; when only a ``low`` candidate exists it is returned as ``low``.
    """
    lk = lookup if lookup is not None else OpenAlexLookup(http, s2_enabled=s2_enabled)
    best: Match | None = None

    # (1) strong identifier -> single record
    doi = ref.doi or (f"{ARXIV_DOI_PREFIX}{ref.arxiv}" if ref.arxiv else None)
    if doi:
        hit = lk.work_by_doi(doi)
        if hit:
            record, extra = hit
            sim = title_similarity(ref.title, record.title)
            author = author_matches(ref.authors, record.authors)
            dy = year_difference(ref.year, record)
            if sim >= 0.4 and (author or not ref.authors):
                return Match(ref, record, sim, author, dy, "high", "doi", extra)
            best = _better(best, evaluate(ref, record, extra, "doi", cfg))
            if best and best.confidence != "low":
                return best

    # (2) OpenAlex autocomplete
    for ac in lk.autocomplete(ref.title)[:8]:
        if title_similarity(ref.title, ac.display_name) < cfg.autocomplete_min_sim:
            continue
        full = lk.work_by_id(ac.id)
        if full is None:
            continue
        best = _better(best, evaluate(ref, full[0], full[1], "openalex_autocomplete", cfg))
        if best and best.confidence == "high":
            return best
    if best and best.confidence == "medium":
        return best

    # (3) Crossref bibliographic search -> DOI -> OpenAlex
    cr_doi = lk.crossref_doi(ref.title, first_author_surname(ref.authors) or None, ref.year)
    if cr_doi:
        full = lk.work_by_doi(cr_doi)
        if full is not None:
            best = _better(best, evaluate(ref, full[0], full[1], "crossref_bibliographic", cfg))
            if best and best.confidence != "low":
                return best

    # (4) Semantic Scholar (only with a key)
    if s2_enabled:
        s2 = lk.s2_doi(ref.title)
        if s2:
            full = lk.work_by_doi(s2)
            if full is not None:
                best = _better(best, evaluate(ref, full[0], full[1], "s2_match", cfg))
    return best


# ---------------------------------------------------------------------------
# OpenAlex / Crossref / S2 request shapes
# ---------------------------------------------------------------------------


def short_openalex_id(value: str | None) -> str | None:
    """``https://openalex.org/W123`` -> ``W123``."""
    if not value:
        return None
    return value.rsplit("/", 1)[-1]


def record_from_openalex(work: Mapping[str, Any]) -> tuple[Record, WorkExtra]:
    """Normalise one OpenAlex ``work`` object (data, never instructions): the shared
    ``sources.openalex.parse_work`` record plus the profile-only :class:`WorkExtra` fields."""
    from kallimachos.sources.openalex import parse_work, short_id

    record = parse_work(work)
    if record is None:  # no title or id: keep what there is so the caller can still report it
        record = Record(
            title=str(work.get("display_name") or work.get("title") or "").strip(),
            openalex_id=short_openalex_id(work.get("id")),
            sources={"openalex"},
        )
    source = (work.get("primary_location") or {}).get("source") or {}
    topics = [
        str(t.get("display_name"))
        for t in (work.get("topics") or [])
        if isinstance(t, dict) and t.get("display_name")
    ]
    subfields: list[str] = []
    for t in work.get("topics") or []:
        sub = t.get("subfield") if isinstance(t, dict) else None
        if isinstance(sub, dict) and sub.get("display_name"):
            name = str(sub["display_name"])
            if name not in subfields:
                subfields.append(name)
    extra = WorkExtra(
        source_id=short_id(source.get("id")),
        cited_by_count=int(work.get("cited_by_count") or 0),
        topics=topics,
        subfields=subfields,
        work_type=str(work.get("type") or ""),
    )
    return record, extra


class OpenAlexLookup:
    """Default :class:`Lookup` over the shared HTTP client."""

    def __init__(self, http: HttpClient, *, s2_enabled: bool = False, max_pages: int = 5) -> None:
        self._http = http
        self._s2_enabled = s2_enabled
        self._max_pages = max_pages
        self._requests: list[str] = []

    @property
    def requests(self) -> list[str]:
        return self._requests

    def _json(
        self, url: str, params: Mapping[str, str] | None = None, *, kind: str = "single"
    ) -> Any:
        query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        self._requests.append(f"{url}?{query}" if query else url)
        resp = self._http.get(url, params=params, kind=kind)
        if resp is None or resp.status != 200:
            return None
        try:
            return json.loads(resp.text())
        except ValueError:
            return None

    def work_by_doi(self, doi: str) -> tuple[Record, WorkExtra] | None:
        data = self._json(f"{OPENALEX_API}/works/doi:{clean_doi(doi)}")
        return record_from_openalex(data) if isinstance(data, dict) and data.get("id") else None

    def work_by_id(self, openalex_id: str) -> tuple[Record, WorkExtra] | None:
        short = short_openalex_id(openalex_id) or openalex_id
        data = self._json(f"{OPENALEX_API}/works/{short}")
        return record_from_openalex(data) if isinstance(data, dict) and data.get("id") else None

    def autocomplete(self, title: str) -> list[AutocompleteHit]:
        data = self._json(
            f"{OPENALEX_API}/autocomplete/works", {"q": _query_text(title)}, kind="search"
        )
        hits: list[AutocompleteHit] = []
        for item in (data or {}).get("results", []) if isinstance(data, dict) else []:
            short = short_openalex_id(item.get("id"))
            if short and item.get("display_name"):
                hits.append(
                    AutocompleteHit(short, str(item["display_name"]), str(item.get("hint") or ""))
                )
        return hits

    def crossref_doi(self, title: str, author: str | None, year: int | None) -> str | None:
        params = {"query.bibliographic": _query_text(title), "rows": "5"}
        if author:
            params["query.author"] = author
        data = self._json(f"{CROSSREF_API}/works", params, kind="search")
        items = (data or {}).get("message", {}).get("items", []) if isinstance(data, dict) else []
        best: tuple[float, str] | None = None
        for item in items:
            titles = item.get("title") or []
            if not titles or not item.get("DOI"):
                continue
            sim = title_similarity(title, str(titles[0]))
            if best is None or sim > best[0]:
                best = (sim, clean_doi(str(item["DOI"])))
        return best[1] if best and best[0] >= 0.7 else None

    def s2_doi(self, title: str) -> str | None:
        if not self._s2_enabled:
            return None
        data = self._json(
            f"{S2_API}/paper/search/match",
            {"query": _query_text(title), "fields": "externalIds,title"},
            kind="search",
        )
        papers = (data or {}).get("data", []) if isinstance(data, dict) else []
        for paper in papers:
            ids = paper.get("externalIds") or {}
            if ids.get("DOI"):
                return clean_doi(str(ids["DOI"]))
            if ids.get("ArXiv"):
                return f"{ARXIV_DOI_PREFIX}{ids['ArXiv']}"
        return None

    def works_by_ids(self, ids: Sequence[str]) -> list[tuple[Record, WorkExtra]]:
        out: list[tuple[Record, WorkExtra]] = []
        shorts = [s for s in (short_openalex_id(i) for i in ids) if s]
        for start in range(0, len(shorts), 50):
            chunk = shorts[start : start + 50]
            data = self._json(
                f"{OPENALEX_API}/works",
                {"filter": "openalex_id:" + "|".join(chunk), "per-page": "50"},
                kind="list",
            )
            for work in (data or {}).get("results", []) if isinstance(data, dict) else []:
                out.append(record_from_openalex(work))
        return out

    def works_by_author(self, author_id: str) -> list[tuple[Record, WorkExtra]]:
        short = short_openalex_id(author_id) or author_id
        out: list[tuple[Record, WorkExtra]] = []
        cursor: str | None = "*"
        pages = 0
        while cursor and pages < self._max_pages:
            data = self._json(
                f"{OPENALEX_API}/works",
                {"filter": f"author.id:{short}", "per-page": "200", "cursor": cursor},
                kind="list",
            )
            if not isinstance(data, dict):
                break
            for work in data.get("results", []):
                out.append(record_from_openalex(work))
            cursor = (data.get("meta") or {}).get("next_cursor")
            pages += 1
        return out


def _query_text(title: str) -> str:
    """Title as a query: whitespace collapsed, control characters removed, 200 chars max."""
    text = re.sub(r"[\x00-\x1f]+", " ", title)
    return re.sub(r"\s+", " ", text).strip()[:200]
