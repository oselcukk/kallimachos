"""Reference importers for ``profile build --from-files``.

Each importer exposes ``parse(path) -> list[ImportedRef]``. Importers never raise on a malformed
entry; they skip it. Shared identifier regexes and the author/kind helpers live here so that
every importer normalises the same way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from kallimachos.interfaces import WorkKind
from kallimachos.records.normalize import collapse_whitespace, match_key

__all__ = [
    "ARXIV_RE",
    "DOI_RE",
    "ImportedRef",
    "clean_doi",
    "find_arxiv",
    "find_doi",
    "first_author_surname",
    "normalize_arxiv",
]

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\]\"'<>,;`{}]+")
"""A DOI anywhere in free text (trailing punctuation is removed by :func:`clean_doi`)."""

ARXIV_RE = re.compile(
    r"(?:arXiv\s*:\s*|arxiv\.org/(?:abs|pdf)/)"
    r"((?:\d{4}\.\d{4,5})|(?:[a-z\-]+(?:\.[A-Z]{2})?/\d{7}))(v\d+)?",
    re.IGNORECASE,
)
"""``arXiv:2510.01234v2``, ``arxiv.org/abs/math/0601001`` (new and old identifier styles)."""

_BARE_ARXIV_RE = re.compile(r"^(?:(\d{4}\.\d{4,5})|([a-z\-]+(?:\.[A-Z]{2})?/\d{7}))(v\d+)?$")


def clean_doi(doi: str) -> str:
    """Strip the URL prefix, trailing punctuation and unbalanced closing brackets; lower-case."""
    value = doi.strip()
    value = re.sub(r"^(?:https?://)?(?:dx\.)?doi\.org/", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^doi:\s*", "", value, flags=re.IGNORECASE)
    value = value.rstrip(".,;:")
    while value.endswith(")") and value.count(")") > value.count("("):
        value = value[:-1]
    return value.rstrip(".,;:").lower()


def find_doi(text: str) -> str | None:
    """First DOI in ``text`` or ``None``."""
    m = DOI_RE.search(text)
    return clean_doi(m.group(0)) if m else None


def normalize_arxiv(value: str) -> str | None:
    """``arXiv:2510.01234v2`` / ``2510.01234`` / ``math/0601001`` -> id without version."""
    text = value.strip()
    m = ARXIV_RE.search(text)
    if m:
        return m.group(1)
    bare = _BARE_ARXIV_RE.match(text)
    if bare:
        return bare.group(1) or bare.group(2)
    return None


def find_arxiv(text: str) -> str | None:
    """First arXiv identifier in ``text`` (prefixed form only, to avoid false hits) or ``None``."""
    m = ARXIV_RE.search(text)
    return m.group(1) if m else None


def first_author_surname(authors: list[str]) -> str:
    """Surname of the first author: ``Last, First`` -> ``Last``; ``First Last`` -> ``Last``."""
    if not authors:
        return ""
    name = collapse_whitespace(authors[0])
    if "," in name:
        return name.split(",", 1)[0].strip()
    parts = name.split(" ")
    return parts[-1] if parts else name


@dataclass
class ImportedRef:
    """A reference extracted from a user file. Only strong-id or long-title article-like refs may
    leave the machine (privacy gate)."""

    title: str
    source_file: str
    year: int | None = None
    authors: list[str] = field(default_factory=list)
    doi: str | None = None
    arxiv: str | None = None
    kind: WorkKind = WorkKind.UNKNOWN
    tags: list[str] = field(default_factory=list)
    project: str | None = None
    venue: str | None = None  # journal / book title as written in the file (local proposals)
    extra_source_files: list[str] = field(default_factory=list)  # filled by ``dedupe``

    @property
    def dedupe_key(self) -> str:
        """``match_key(title)[:60] | first_author[:5]``."""
        title = match_key(self.title)[:60]
        author = match_key(first_author_surname(self.authors))[:5]
        return f"{title}|{author}"

    @property
    def source_files(self) -> list[str]:
        """Primary source file first, then the files merged in by ``dedupe``."""
        out = [self.source_file]
        out.extend(f for f in self.extra_source_files if f not in out)
        return out
