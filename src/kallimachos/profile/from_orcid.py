"""Profile from an ORCID iD or an OpenAlex author id.

The person's works become the seeds; the proposal step is shared with the files path
(:func:`kallimachos.profile.from_files.profile_from_matches`). ORCID's public API needs no
token; ``sources.orcid.token_ref`` is left to the HTTP layer when it is set.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from kallimachos.config.schema import Config
from kallimachos.interfaces import HttpClient, Record, UsageError, WorkKind
from kallimachos.profile.importers import ImportedRef, clean_doi, normalize_arxiv
from kallimachos.profile.match import Lookup, Match, OpenAlexLookup, WorkExtra, match_reference

ORCID_WORKS_URL = "https://pub.orcid.org/v3.0/{orcid}/works"
ORCID_RE = r"^[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9X]$"
OPENALEX_AUTHOR_RE = r"^A[0-9]{4,}$"

_ORCID_KIND: dict[str, WorkKind] = {
    "journal-article": WorkKind.ARTICLE,
    "conference-paper": WorkKind.ARTICLE,
    "book": WorkKind.BOOK,
    "edited-book": WorkKind.BOOK,
    "book-chapter": WorkKind.CHAPTER,
    "dissertation-thesis": WorkKind.THESIS,
    "dissertation": WorkKind.THESIS,
    "preprint": WorkKind.PREPRINT,
    "working-paper": WorkKind.PREPRINT,
}


def normalize_orcid(value: str) -> str:
    """``https://orcid.org/0000-0002-1825-0097`` -> ``0000-0002-1825-0097`` (validated)."""
    orcid = value.strip().rsplit("/", 1)[-1].upper()
    if not re.match(ORCID_RE, orcid):
        raise UsageError(f"not an ORCID iD: {value!r}")
    return orcid


def works_from_orcid(
    orcid: str, config: Config, http: HttpClient, *, request_log: list[str] | None = None
) -> list[Record]:
    """Public ORCID works summary -> records (title, year, DOI/arXiv, venue, kind)."""
    orcid = normalize_orcid(orcid)
    url = ORCID_WORKS_URL.format(orcid=orcid)
    if request_log is not None:
        request_log.append(url)
    resp = http.get(url, headers={"Accept": "application/json"}, kind="single")
    if resp is None or resp.status != 200:
        return []
    try:
        data = json.loads(resp.text())
    except ValueError:
        return []
    return records_from_orcid_json(data)


def records_from_orcid_json(data: Mapping[str, Any]) -> list[Record]:
    """Parse the ``/works`` summary document (data, never instructions)."""
    out: list[Record] = []
    for group in data.get("group") or []:
        summaries = group.get("work-summary") or []
        if not summaries:
            continue
        summary = summaries[0]
        title = _value(summary.get("title"), "title")
        if not title:
            continue
        year_text = _value(summary.get("publication-date"), "year")
        published = None
        if year_text and year_text.isdigit():
            published = _dt.date(int(year_text), 1, 1)
        doi: str | None = None
        arxiv: str | None = None
        ids = (summary.get("external-ids") or {}).get("external-id") or []
        for ext in [*ids, *((group.get("external-ids") or {}).get("external-id") or [])]:
            id_type = str(ext.get("external-id-type") or "").lower()
            value = str(ext.get("external-id-value") or "")
            if id_type == "doi" and not doi:
                doi = clean_doi(value)
            elif id_type == "arxiv" and not arxiv:
                arxiv = normalize_arxiv(value)
        kind = _ORCID_KIND.get(str(summary.get("type") or "").lower(), WorkKind.UNKNOWN)
        if kind is WorkKind.UNKNOWN and arxiv:
            kind = WorkKind.PREPRINT
        venue = _value(summary.get("journal-title")) or None
        out.append(
            Record(
                title=title,
                venue=venue,
                published=published,
                doi=doi,
                arxiv_id=arxiv,
                kind=kind,
                sources={"orcid"},
            )
        )
    return out


def _value(node: Any, key: str | None = None) -> str:
    if not isinstance(node, dict):
        return ""
    inner = node.get(key) if key else node
    if isinstance(inner, dict):
        value = inner.get("value")
        return str(value).strip() if value is not None else ""
    return str(inner).strip() if inner is not None else ""


def normalize_openalex_author(value: str) -> str:
    """``https://openalex.org/A5000000001`` -> ``A5000000001`` (validated)."""
    short = value.strip().rsplit("/", 1)[-1].upper()
    if not re.match(OPENALEX_AUTHOR_RE, short):
        raise UsageError(f"not an OpenAlex author id: {value!r}")
    return short


def works_from_openalex_author(
    author_id: str, config: Config, http: HttpClient, *, lookup: Lookup | None = None
) -> list[Record]:
    """All works of an OpenAlex author (``author.id`` filter, cursor paging)."""
    short = normalize_openalex_author(author_id)
    lk = lookup if lookup is not None else OpenAlexLookup(http)
    return [record for record, _extra in lk.works_by_author(short)]


def refs_from_records(records: Sequence[Record], source_label: str) -> list[ImportedRef]:
    """Records of the person -> references, so the shared matcher can enrich them."""
    refs: list[ImportedRef] = []
    for record in records:
        refs.append(
            ImportedRef(
                title=record.title,
                source_file=source_label,
                year=record.published.year if record.published else None,
                authors=[a.name for a in record.authors],
                doi=record.doi,
                arxiv=record.arxiv_id,
                kind=record.kind,
                venue=record.venue,
            )
        )
    return refs


def match_person_works(
    records: Sequence[Record],
    config: Config,
    http: HttpClient,
    *,
    lookup: Lookup | None,
    source_label: str,
    via_hint: str,
) -> tuple[list[Match], list[ImportedRef]]:
    """Enrich each work through OpenAlex (DOI first). Works that cannot be enriched stay as
    local references (seeds with ``low`` confidence when they carry a strong id)."""
    matches: list[Match] = []
    unmatched: list[ImportedRef] = []
    for ref in refs_from_records(records, source_label):
        found = match_reference(ref, http, config.profile.match, lookup=lookup)
        if found is None:
            unmatched.append(ref)
            continue
        if via_hint in ("orcid", "openalex_author"):
            found.via = "orcid" if via_hint == "orcid" else "openalex_author"
        matches.append(found)
    return matches, unmatched


def matches_from_openalex_records(
    pairs: Sequence[tuple[Record, WorkExtra]], source_label: str
) -> list[Match]:
    """OpenAlex author works are already full records: wrap them as high-confidence matches."""
    out: list[Match] = []
    for record, extra in pairs:
        ref = refs_from_records([record], source_label)[0]
        out.append(
            Match(
                ref=ref,
                record=record,
                sim=1.0,
                author_match=True,
                year_diff=0,
                confidence="high",
                via="openalex_author",
                extra=extra,
            )
        )
    return out
