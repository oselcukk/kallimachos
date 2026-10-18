"""Record pool: merge the same work from several sources.

Merge rules: longest abstract wins; when a journal version exists, doi/venue/url/dates come from
the journal record; ``sources`` and ``queries`` are unions.

Two records are the same work when they share any pool key (``records.keys.keys_for``). When an
incoming record bridges two pooled records (one key each), all three are merged into one.
"""

from __future__ import annotations

import dataclasses

from kallimachos.interfaces import Record, WorkKind
from kallimachos.records.keys import keys_for
from kallimachos.records.model import ARXIV_DOI_PREFIX, normalize_doi


def is_journal_version(record: Record) -> bool:
    """A published (journal/book) version: kind ``article``/``chapter``/``book`` or a non-arXiv
    DOI with a venue on a record that is not explicitly a preprint."""
    if record.kind in (WorkKind.ARTICLE, WorkKind.CHAPTER, WorkKind.BOOK):
        return True
    if record.kind is WorkKind.PREPRINT:
        return False
    doi = normalize_doi(record.doi)
    return bool(doi and not doi.startswith(ARXIV_DOI_PREFIX) and record.venue)


def _union(a: list[str], b: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in [*a, *b]:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


_PUBLICATION_FIELDS = ("doi", "venue", "url", "published", "created")
_ID_FIELDS = (
    "arxiv_id",
    "eprint_id",
    "openalex_id",
    "zbmath_id",
    "pubmed_id",
    "s2_id",
)


def merge(existing: Record, incoming: Record) -> Record:
    """Merge two records of the same work (rules in the module docstring).

    Neither input is modified; the result is a new :class:`Record`. ``existing`` wins every tie.
    """
    out = dataclasses.replace(
        existing,
        authors=list(existing.authors),
        issn=list(existing.issn),
        categories=list(existing.categories),
        msc=list(existing.msc),
        keywords=list(existing.keywords),
        referenced_works=list(existing.referenced_works),
        sources=set(existing.sources),
        queries=set(existing.queries),
    )

    # publication fields: the journal version supplies doi/venue/url/dates
    journal_in = is_journal_version(incoming)
    journal_ex = is_journal_version(existing)
    take_from_incoming = journal_in and not journal_ex
    for name in _PUBLICATION_FIELDS:
        mine, theirs = getattr(out, name), getattr(incoming, name)
        if theirs is not None and (mine is None or take_from_incoming):
            setattr(out, name, theirs)
    if take_from_incoming and incoming.issn:
        out.issn = _union(incoming.issn, out.issn)
    else:
        out.issn = _union(out.issn, incoming.issn)
    # identifiers: fill gaps
    for name in _ID_FIELDS:
        if getattr(out, name) is None and getattr(incoming, name) is not None:
            setattr(out, name, getattr(incoming, name))

    # longest abstract wins (ties keep the existing one)
    if incoming.abstract and len(incoming.abstract.strip()) > len((out.abstract or "").strip()):
        out.abstract = incoming.abstract
        out.abstract_source = incoming.abstract_source

    if not out.title and incoming.title:
        out.title = incoming.title
    if len(incoming.authors) > len(out.authors):
        out.authors = list(incoming.authors)
    else:  # fill ORCID / OpenAlex ids of matching author names
        for mine_a, theirs_a in zip(out.authors, incoming.authors, strict=False):
            if mine_a.name.strip().lower() == theirs_a.name.strip().lower():
                if mine_a.orcid is None and theirs_a.orcid:
                    mine_a.orcid = theirs_a.orcid
                if mine_a.openalex_id is None and theirs_a.openalex_id:
                    mine_a.openalex_id = theirs_a.openalex_id

    out.primary_category = out.primary_category or incoming.primary_category
    out.categories = _union(out.categories, incoming.categories)
    out.msc = _union(out.msc, incoming.msc)
    out.keywords = _union(out.keywords, incoming.keywords)
    out.referenced_works = _union(out.referenced_works, incoming.referenced_works)

    if journal_in and not journal_ex:
        out.kind = incoming.kind if incoming.kind is not WorkKind.UNKNOWN else WorkKind.ARTICLE
    elif out.kind is WorkKind.UNKNOWN:
        out.kind = incoming.kind

    out.sources = existing.sources | incoming.sources
    out.queries = existing.queries | incoming.queries
    return out


class Pool:
    def __init__(self) -> None:
        self._by_key: dict[str, Record] = {}
        self._records: list[Record] = []

    def add(self, record: Record) -> Record:
        """Insert or merge; return the pooled record."""
        keys = keys_for(record)
        matches: list[Record] = []
        for key in keys:
            found = self._by_key.get(key)
            if found is not None and all(found is not m for m in matches):
                matches.append(found)
        if not matches:
            self._records.append(record)
            for key in keys:
                self._by_key[key] = record
            return record

        # merge in pool order so the earliest record stays the "existing" side
        matches.sort(key=lambda r: next(i for i, x in enumerate(self._records) if x is r))
        merged = matches[0]
        for other in matches[1:]:
            merged = merge(merged, other)
        merged = merge(merged, record)

        first_index = next(i for i, x in enumerate(self._records) if x is matches[0])
        self._records = [r for r in self._records if all(r is not m for m in matches)]
        self._records.insert(min(first_index, len(self._records)), merged)
        stale = [k for k, v in self._by_key.items() if any(v is m for m in matches)]
        for key in stale:
            self._by_key[key] = merged
        for key in keys_for(merged):
            self._by_key[key] = merged
        return merged

    def get(self, key: str) -> Record | None:
        """Pooled record for a pool key, if any."""
        return self._by_key.get(key)

    def records(self) -> list[Record]:
        return list(self._records)

    def __len__(self) -> int:
        return len(self._records)
