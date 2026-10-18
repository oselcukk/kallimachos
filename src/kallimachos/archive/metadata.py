"""Metadata resolution for archive triggers.

Order: OpenAlex single work (DOI or W id) -> arXiv source -> arXiv abs page -> ePrint source ->
ePrint page -> trigger hint. Source failures never stop the chain. The abs/ePrint page fallbacks
read only the ``citation_*`` ``<meta>`` tags of the public landing page.
"""

from __future__ import annotations

import datetime as _dt
import html
import re
from collections.abc import Mapping

from kallimachos.archive.identity import key_of, normalize_doi, split_arxiv_version
from kallimachos.interfaces import (
    ArchiveTrigger,
    Author,
    HttpClient,
    Ident,
    IdentKind,
    Record,
    Source,
    WorkKind,
)

ARXIV_ABS_URL = "https://arxiv.org/abs/{id}"
EPRINT_URL = "https://eprint.iacr.org/{id}"
ARXIV_BATCH = 50  # arXiv id_list batch size used by the arXiv source

_META_RE = re.compile(
    r"<meta\s+(?:name|property)=\"(citation_[a-z_]+)\"\s+content=\"([^\"]*)\"", re.IGNORECASE
)
_META_RE_REV = re.compile(
    r"<meta\s+content=\"([^\"]*)\"\s+(?:name|property)=\"(citation_[a-z_]+)\"", re.IGNORECASE
)


def resolve_metadata(
    trigger: ArchiveTrigger,
    sources: Mapping[str, Source],
    http: HttpClient,
    *,
    hint: Record | None = None,
) -> Record | None:
    """OpenAlex single -> arXiv id_list batches (50) -> abs page fallback -> ePrint page -> trigger
    hint."""
    key = key_of(trigger.ident)
    if key is None:
        return hint
    ident = Ident.parse_key(key)
    record: Record | None = None
    if ident.kind in (IdentKind.DOI, IdentKind.OPENALEX):
        record = _from_source(sources, "openalex", ident, http)
    elif ident.kind is IdentKind.ARXIV:
        record = _from_source(sources, "arxiv", ident, http)
        if record is None:
            record = arxiv_abs_page(ident.value, http)
    elif ident.kind is IdentKind.EPRINT:
        record = _from_source(sources, "iacr_eprint", ident, http)
        if record is None:
            record = eprint_page(ident.value, http)
    if record is None or not record.title.strip():
        return hint
    return record


def _from_source(
    sources: Mapping[str, Source], name: str, ident: Ident, http: HttpClient
) -> Record | None:
    source = sources.get(name)
    if source is None:
        return None
    try:
        return source.metadata(ident, http)
    except Exception:  # a failing source never stops the archive run
        return None


def citation_meta(page: str) -> dict[str, list[str]]:
    """``citation_*`` meta tags of an HTML page (Highwire/Google Scholar convention)."""
    out: dict[str, list[str]] = {}
    for name, value in _META_RE.findall(page):
        out.setdefault(name.lower(), []).append(html.unescape(value).strip())
    for value, name in _META_RE_REV.findall(page):
        out.setdefault(name.lower(), []).append(html.unescape(value).strip())
    return out


def _parse_date(value: str | None) -> _dt.date | None:
    if not value:
        return None
    m = re.match(r"(\d{4})(?:[/-](\d{1,2}))?(?:[/-](\d{1,2}))?", value)
    if not m:
        return None
    try:
        return _dt.date(int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1))
    except ValueError:
        return None


def _first(meta: dict[str, list[str]], name: str) -> str | None:
    values = meta.get(name)
    return values[0] if values else None


def record_from_meta(meta: dict[str, list[str]], *, source: str) -> Record | None:
    title = (meta.get("citation_title") or [""])[0]
    if not title:
        return None
    date = _parse_date(_first(meta, "citation_date") or _first(meta, "citation_publication_date"))
    online = _parse_date(_first(meta, "citation_online_date"))
    doi = _first(meta, "citation_doi")
    return Record(
        title=title,
        authors=[Author(name=n) for n in meta.get("citation_author", [])],
        doi=normalize_doi(doi) if doi else None,
        published=date,
        created=online or date,
        sources={source},
    )


def arxiv_abs_page(arxiv_id: str, http: HttpClient) -> Record | None:
    base, version = split_arxiv_version(arxiv_id)
    resp = http.get(ARXIV_ABS_URL.format(id=base), kind="single")
    if resp is None or resp.status != 200:
        return None
    rec = record_from_meta(citation_meta(resp.text()), source="arxiv_abs")
    if rec is None:
        return None
    rec.arxiv_id = base + (version or "")
    rec.kind = WorkKind.PREPRINT
    rec.url = ARXIV_ABS_URL.format(id=base)
    return rec


def eprint_page(eprint_id: str, http: HttpClient) -> Record | None:
    resp = http.get(EPRINT_URL.format(id=eprint_id), kind="single")
    if resp is None or resp.status != 200:
        return None
    rec = record_from_meta(citation_meta(resp.text()), source="eprint_page")
    if rec is None:
        return None
    rec.eprint_id = eprint_id
    rec.kind = WorkKind.PREPRINT
    rec.url = EPRINT_URL.format(id=eprint_id)
    return rec
