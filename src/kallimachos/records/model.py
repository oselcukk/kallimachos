"""Record helpers. The dataclass itself lives in ``interfaces``."""

from __future__ import annotations

import re

from kallimachos.interfaces import Author, Ident, IdentKind, Record
from kallimachos.records.normalize import collapse_whitespace, match_key

__all__ = [
    "Author",
    "Record",
    "arxiv_id_from_doi",
    "normalize_arxiv_id",
    "normalize_doi",
    "primary_ident",
    "strong_idents",
    "title_ident",
]

ARXIV_DOI_PREFIX = "10.48550/"
TITLE_KEY_MIN_CHARS = 20
"""``nt:`` keys only for titles whose match key is longer than this."""

_DOI_PREFIXES = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_ARXIV_PREFIXES = re.compile(r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:\s*)", re.IGNORECASE)
_ARXIV_VERSION = re.compile(r"v\d+$")
_OPENALEX_PREFIX = re.compile(r"^(?:https?://openalex\.org/)", re.IGNORECASE)


def normalize_doi(doi: str | None) -> str | None:
    """Lower-case DOI without resolver prefix; ``None`` for empty input."""
    if not doi:
        return None
    value = _DOI_PREFIXES.sub("", doi.strip()).strip().lower()
    return value or None


def normalize_arxiv_id(arxiv_id: str | None) -> str | None:
    """Version-less arXiv id without ``arXiv:``/URL prefix and ``.pdf`` suffix."""
    if not arxiv_id:
        return None
    value = _ARXIV_PREFIXES.sub("", arxiv_id.strip()).strip()
    value = value.removesuffix(".pdf")
    value = _ARXIV_VERSION.sub("", value)
    return value or None


def arxiv_id_from_doi(doi: str | None) -> str | None:
    """``10.48550/arXiv.2510.01234`` -> ``2510.01234``; other DOIs -> ``None``."""
    norm = normalize_doi(doi)
    if not norm or not norm.startswith(ARXIV_DOI_PREFIX):
        return None
    rest = norm[len(ARXIV_DOI_PREFIX) :]
    rest = rest.removeprefix("arxiv.")
    return normalize_arxiv_id(rest)


def _openalex(value: str | None) -> str | None:
    if not value:
        return None
    v = _OPENALEX_PREFIX.sub("", value.strip())
    if v and v[0] in "wW" and v[1:].isdigit():
        return "W" + v[1:]
    return v or None


def _plain(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip()
    return v or None


def strong_idents(record: Record) -> list[Ident]:
    """DOI (excluding the arXiv DOI prefix), arXiv, ePrint, OpenAlex, zbMATH, PubMed, S2 in that
    order.

    An arXiv DOI (``10.48550/arXiv.<id>``) is turned into an arXiv identifier when the record has
    no ``arxiv_id`` of its own, so the DataCite DOI of a preprint merges with the arXiv record.
    """
    out: list[Ident] = []
    doi = normalize_doi(record.doi)
    if doi and not doi.startswith(ARXIV_DOI_PREFIX):
        out.append(Ident(IdentKind.DOI, doi))
    arxiv = normalize_arxiv_id(record.arxiv_id) or arxiv_id_from_doi(record.doi)
    if arxiv:
        out.append(Ident(IdentKind.ARXIV, arxiv))
    for kind, value in (
        (IdentKind.EPRINT, _plain(record.eprint_id)),
        (IdentKind.OPENALEX, _openalex(record.openalex_id)),
        (IdentKind.ZBMATH, _plain(record.zbmath_id)),
        (IdentKind.PUBMED, _plain(record.pubmed_id)),
        (IdentKind.S2, _plain(record.s2_id)),
    ):
        if value:
            out.append(Ident(kind, value))
    return out


def title_ident(record: Record) -> Ident | None:
    """``nt:<match_key(title)>`` when the key is longer than 20 characters, else ``None``."""
    key = match_key(collapse_whitespace(record.title or ""))
    if len(key) > TITLE_KEY_MIN_CHARS:
        return Ident(IdentKind.TITLE, key)
    return None


def primary_ident(record: Record) -> Ident | None:
    """First strong identifier, else a title identifier when the title is long enough, else
    ``None``."""
    strong = strong_idents(record)
    if strong:
        return strong[0]
    return title_ident(record)
