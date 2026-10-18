"""RIS records: ``TY/AU/TI/PY/DO``.

Tag lines look like ``TY  - JOUR``; a record ends with ``ER  -``. Continuation lines (no tag)
are appended to the previous value.
"""

from __future__ import annotations

import re
from pathlib import Path

from kallimachos.interfaces import WorkKind
from kallimachos.profile.importers import (
    ImportedRef,
    clean_doi,
    find_arxiv,
    find_doi,
)
from kallimachos.records.normalize import collapse_whitespace

_TAG = re.compile(r"^([A-Z][A-Z0-9])  - ?(.*)$")
_YEAR = re.compile(r"(1[6-9]\d\d|20\d\d)")
_KIND: dict[str, WorkKind] = {
    "JOUR": WorkKind.ARTICLE,
    "JFULL": WorkKind.ARTICLE,
    "EJOUR": WorkKind.ARTICLE,
    "CPAPER": WorkKind.ARTICLE,
    "CONF": WorkKind.ARTICLE,
    "BOOK": WorkKind.BOOK,
    "EBOOK": WorkKind.BOOK,
    "EDBOOK": WorkKind.BOOK,
    "THES": WorkKind.THESIS,
    "CHAP": WorkKind.CHAPTER,
    "ECHAP": WorkKind.CHAPTER,
    "UNPB": WorkKind.PREPRINT,
}


def parse(path: Path) -> list[ImportedRef]:
    """Parse one file; never raises on malformed entries, skips them."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[ImportedRef] = []
    for record in _records(text):
        try:
            ref = _ref_from_record(record, str(path))
        except Exception:  # a malformed record must never stop the import
            ref = None
        if ref is not None:
            out.append(ref)
    return out


def _records(text: str) -> list[dict[str, list[str]]]:
    records: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] = {}
    last_tag: str | None = None
    for raw in text.splitlines():
        line = raw.lstrip("﻿").rstrip()
        m = _TAG.match(line)
        if not m:
            if last_tag and line.strip() and current.get(last_tag):
                current[last_tag][-1] += " " + line.strip()
            continue
        tag, value = m.group(1), m.group(2).strip()
        if tag == "ER":
            if current:
                records.append(current)
            current, last_tag = {}, None
            continue
        current.setdefault(tag, []).append(value)
        last_tag = tag
    if current:
        records.append(current)
    return records


def _first(record: dict[str, list[str]], *tags: str) -> str:
    for tag in tags:
        for value in record.get(tag, []):
            if value:
                return value
    return ""


def _ref_from_record(record: dict[str, list[str]], source_file: str) -> ImportedRef | None:
    title = collapse_whitespace(_first(record, "TI", "T1", "BT", "CT"))
    if not title:
        return None
    type_code = _first(record, "TY").upper()
    kind = _KIND.get(type_code, WorkKind.UNKNOWN)
    authors = [collapse_whitespace(a) for a in record.get("AU", []) + record.get("A1", []) if a]
    year_m = _YEAR.search(_first(record, "PY", "Y1", "DA"))
    year = int(year_m.group(1)) if year_m else None
    doi_raw = _first(record, "DO", "DI")
    doi = clean_doi(doi_raw) if doi_raw else None
    joined = " ".join(v for values in record.values() for v in values)
    if doi is None:
        doi = find_doi(" ".join(record.get("UR", []) + record.get("L1", []) + record.get("N1", [])))
    arxiv = find_arxiv(joined)
    if kind is WorkKind.UNKNOWN and arxiv:
        kind = WorkKind.PREPRINT
    venue = collapse_whitespace(_first(record, "JO", "JF", "T2", "J2", "PB"))
    tags = [collapse_whitespace(k) for k in record.get("KW", []) if k.strip()]
    return ImportedRef(
        title=title,
        source_file=source_file,
        year=year,
        authors=authors,
        doi=doi,
        arxiv=arxiv,
        kind=kind,
        tags=tags,
        venue=venue or None,
    )
