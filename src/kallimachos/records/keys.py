"""Pool keys and candidate ids.

Keys: ``doi:`` (not ``10.48550``), ``arxiv:`` (version-less), ``eprint:``, ``oa:``, ``zb:``,
``pmid:``, ``nt:<match_key(title)>`` when the key is longer than 20 characters.
"""

from __future__ import annotations

import hashlib

from kallimachos.interfaces import Record
from kallimachos.records.model import (
    ARXIV_DOI_PREFIX,
    TITLE_KEY_MIN_CHARS,
    normalize_arxiv_id,
    strong_idents,
    title_ident,
)
from kallimachos.records.normalize import match_key

__all__ = [
    "ARXIV_DOI_PREFIX",
    "TITLE_KEY_MIN_CHARS",
    "candidate_id",
    "keys_for",
    "strip_arxiv_version",
]


def keys_for(record: Record) -> list[str]:
    """Every pool key of the record, strongest first.

    The title key is always added (when long enough) so that a preprint and its journal version,
    which carry different strong identifiers, still meet in the pool.
    """
    keys = [ident.key for ident in strong_idents(record)]
    title = title_ident(record)
    if title is not None:
        keys.append(title.key)
    return keys


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def candidate_id(record: Record) -> str:
    """Display id (``arXiv:<id>``, ``doi:<doi>``, ``W<id>``, ``rss:<sha8>`` ...).

    Without a strong identifier the id is ``rss:<sha8(url)>`` (feeds, alerts) or, without a URL,
    ``nt:<sha8(match_key(title))>``.
    """
    strong = strong_idents(record)
    if strong:
        return strong[0].candidate_id
    if record.url:
        return f"rss:{_sha8(record.url.strip())}"
    return f"nt:{_sha8(match_key(record.title or ''))}"


def strip_arxiv_version(arxiv_id: str) -> str:
    """``2510.01234v2`` -> ``2510.01234``."""
    return normalize_arxiv_id(arxiv_id) or ""
