"""Identifier parsing and identity keys for the archive.

Keys use the pool-key form of :class:`~kallimachos.interfaces.Ident` (``doi:10.1/x``,
``arxiv:2501.01234`` without version, ``eprint:2024/123``, ``oa:W123``). arXiv DOIs
(``10.48550/arXiv.<id>``) become arXiv keys, never DOI keys.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from kallimachos.interfaces import CatalogEntry, Ident, IdentKind, Record
from kallimachos.records import model as record_model

ARXIV_DOI_PREFIX = record_model.ARXIV_DOI_PREFIX

_DOI_RE = re.compile(r"\b(10\.[0-9]{4,9}/[^\s\"<>]+)", re.IGNORECASE)
_ARXIV_NEW_RE = re.compile(r"^(?P<id>[0-9]{4}\.[0-9]{4,5})(?P<version>v[0-9]+)?$")
_ARXIV_OLD_RE = re.compile(r"^(?P<id>[a-z-]+(?:\.[A-Z]{2})?/[0-9]{7})(?P<version>v[0-9]+)?$")
_EPRINT_RE = re.compile(r"^(?P<id>(?:19|20)[0-9]{2}/[0-9]{3,5})$")
_OPENALEX_RE = re.compile(r"^(?:https?://openalex\.org/)?(?P<id>W[0-9]+)$", re.IGNORECASE)
_TRAILING = ".,;)]}>"


def normalize_doi(doi: str) -> str:
    """Lower-case DOI without resolver prefix (``records.model.normalize_doi``) or trailing
    punctuation picked up from free text."""
    return (record_model.normalize_doi(doi) or "").rstrip(_TRAILING)


def split_arxiv_version(arxiv_id: str) -> tuple[str, str | None]:
    """``2501.01234v2`` -> (``2501.01234``, ``v2``); ids without version return ``None``."""
    value = arxiv_id.strip()
    if value.lower().startswith("arxiv:"):
        value = value[6:]
    m = re.match(r"^(.*?)(v[0-9]+)$", value)
    if m and (_ARXIV_NEW_RE.match(value) or _ARXIV_OLD_RE.match(value)):
        return m.group(1), m.group(2)
    return value, None


def arxiv_from_doi(doi: str) -> str | None:
    """arXiv id hidden in an arXiv DOI (``10.48550/arXiv.2501.01234``); one implementation in
    ``records.model.arxiv_id_from_doi``."""
    return record_model.arxiv_id_from_doi(normalize_doi(doi))


def parse_ident(text: str) -> Ident | None:
    """Recognise a DOI, arXiv id (new/old, ``arXiv:`` prefix, abs/pdf URL), IACR ePrint id
    (``YYYY/NNNN`` or eprint URL), OpenAlex work id or a pool key / candidate id."""
    value = text.strip().rstrip(_TRAILING)
    if not value:
        return None
    low = value.lower()
    for marker in ("arxiv.org/abs/", "arxiv.org/pdf/"):
        if marker in low:
            value = value[low.index(marker) + len(marker) :].removesuffix(".pdf")
            return _arxiv_ident(value)
    if "eprint.iacr.org/" in low:
        value = value[low.index("eprint.iacr.org/") + len("eprint.iacr.org/") :]
        value = value.removesuffix(".pdf")
        m = _EPRINT_RE.match(value)
        return Ident(IdentKind.EPRINT, m.group("id")) if m else None
    if "doi.org/" in low or low.startswith("doi:"):
        return _doi_ident(value)
    if low.startswith("arxiv:"):
        return _arxiv_ident(value[6:])
    if low.startswith("eprint:"):
        m = _EPRINT_RE.match(value[7:])
        return Ident(IdentKind.EPRINT, m.group("id")) if m else None
    if low.startswith("oa:"):
        value = value[3:]
    m_oa = _OPENALEX_RE.match(value)
    if m_oa:
        return Ident(IdentKind.OPENALEX, m_oa.group("id").upper())
    if low.startswith("10."):
        return _doi_ident(value)
    if _ARXIV_NEW_RE.match(value) or _ARXIV_OLD_RE.match(value):
        return _arxiv_ident(value)
    m_ep = _EPRINT_RE.match(value)
    if m_ep:
        return Ident(IdentKind.EPRINT, m_ep.group("id"))
    return None


def _doi_ident(value: str) -> Ident | None:
    doi = normalize_doi(value)
    if not _DOI_RE.fullmatch(doi):
        return None
    arxiv = arxiv_from_doi(doi)
    if arxiv:
        return _arxiv_ident(arxiv)
    return Ident(IdentKind.DOI, doi)


def _arxiv_ident(value: str) -> Ident | None:
    base, _version = split_arxiv_version(value)
    if _ARXIV_NEW_RE.match(base) or _ARXIV_OLD_RE.match(base):
        return Ident(IdentKind.ARXIV, base)
    return None


def find_dois(text: str) -> list[str]:
    """Every DOI in free text, normalised, order kept, duplicates removed."""
    out: list[str] = []
    for m in _DOI_RE.finditer(text):
        doi = normalize_doi(m.group(1))
        if doi not in out:
            out.append(doi)
    return out


def key_of(text: str) -> str | None:
    """Pool key of a trigger ident (candidate id, key, URL or bare id)."""
    ident = parse_ident(text)
    if ident is None:
        for parser in (Ident.parse_key, Ident.parse_candidate_id):
            try:
                ident = parser(text.strip())
                break
            except ValueError:
                continue
    return normalize_key(ident.key) if ident is not None else None


def normalize_key(key: str) -> str:
    """Canonical form of a pool key: DOI lower-case, arXiv without version, W upper-case."""
    try:
        ident = Ident.parse_key(key)
    except ValueError:
        return key
    if ident.kind is IdentKind.DOI:
        doi = normalize_doi(ident.value)
        arxiv = arxiv_from_doi(doi)
        return f"arxiv:{arxiv}" if arxiv else f"doi:{doi}"
    if ident.kind is IdentKind.ARXIV:
        return f"arxiv:{split_arxiv_version(ident.value)[0]}"
    if ident.kind is IdentKind.OPENALEX:
        value = ident.value.upper()
        return f"oa:{value if value.startswith('W') else 'W' + value}"
    return ident.key


def journal_doi(record: Record) -> str | None:
    """The record's DOI unless it is an arXiv DOI."""
    if record.doi and not arxiv_from_doi(record.doi):
        return normalize_doi(record.doi)
    return None


def record_keys(record: Record) -> list[str]:
    """Identity keys of a record: the strong pool keys of ``records.model.strong_idents`` (DOI
    without the arXiv prefix, arXiv without version, ePrint, OpenAlex, zbMATH, PubMed, S2) in
    canonical form. The ``nt:`` title key is a pool-only key and never an archive identity."""
    return _unique(normalize_key(ident.key) for ident in record_model.strong_idents(record))


def entry_keys(entry: CatalogEntry) -> list[str]:
    keys = [normalize_key(k) for k in entry.keys]
    if entry.doi:
        keys.append(normalize_key(f"doi:{entry.doi}"))
    if entry.arxiv:
        keys.append(f"arxiv:{split_arxiv_version(entry.arxiv)[0]}")
    if entry.eprint:
        keys.append(f"eprint:{entry.eprint}")
    if entry.openalex_id:
        keys.append(normalize_key(f"oa:{entry.openalex_id}"))
    return _unique(keys)


def _unique(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out
