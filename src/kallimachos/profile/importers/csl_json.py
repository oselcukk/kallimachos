"""CSL-JSON (Zotero export): ``DOI, title, author, issued``.

Accepts a JSON array of items or an object with an ``items`` list. Malformed items are skipped.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kallimachos.interfaces import WorkKind
from kallimachos.profile.importers import (
    ImportedRef,
    clean_doi,
    find_arxiv,
    find_doi,
    normalize_arxiv,
)
from kallimachos.records.normalize import collapse_whitespace

_KIND: dict[str, WorkKind] = {
    "article-journal": WorkKind.ARTICLE,
    "article": WorkKind.ARTICLE,
    "paper-conference": WorkKind.ARTICLE,
    "book": WorkKind.BOOK,
    "thesis": WorkKind.THESIS,
    "chapter": WorkKind.CHAPTER,
    "manuscript": WorkKind.PREPRINT,
    "preprint": WorkKind.PREPRINT,
}


def parse(path: Path) -> list[ImportedRef]:
    """Parse one file; never raises on malformed entries, skips them."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items: Any = data.get("items", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out: list[ImportedRef] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            ref = _ref_from_item(item, str(path))
        except Exception:  # a malformed item must never stop the import
            ref = None
        if ref is not None:
            out.append(ref)
    return out


def _author_name(author: dict[str, Any]) -> str:
    if author.get("literal"):
        return collapse_whitespace(str(author["literal"]))
    family = collapse_whitespace(str(author.get("family", "")))
    given = collapse_whitespace(str(author.get("given", "")))
    if family and given:
        return f"{family}, {given}"
    return family or given


def _year(item: dict[str, Any]) -> int | None:
    issued = item.get("issued")
    if isinstance(issued, dict):
        parts = issued.get("date-parts")
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            try:
                return int(parts[0][0])
            except (TypeError, ValueError):
                return None
        raw = issued.get("raw") or issued.get("literal")
        if isinstance(raw, str) and raw[:4].isdigit():
            return int(raw[:4])
    return None


def _ref_from_item(item: dict[str, Any], source_file: str) -> ImportedRef | None:
    title = collapse_whitespace(str(item.get("title", "") or ""))
    if not title:
        return None
    authors = [
        _author_name(a) for a in item.get("author", []) if isinstance(a, dict) and _author_name(a)
    ]
    doi = clean_doi(str(item["DOI"])) if item.get("DOI") else None
    if doi is None and item.get("URL"):
        doi = find_doi(str(item["URL"]))
    arxiv: str | None = None
    for key in ("URL", "note", "archive_location", "number"):
        value = item.get(key)
        if isinstance(value, str):
            arxiv = find_arxiv(value) or (
                normalize_arxiv(value) if key == "archive_location" else None
            )
            if arxiv:
                break
    kind = _KIND.get(str(item.get("type", "")).lower(), WorkKind.UNKNOWN)
    if kind is WorkKind.UNKNOWN and arxiv:
        kind = WorkKind.PREPRINT
    venue = collapse_whitespace(str(item.get("container-title") or ""))
    keywords = item.get("keyword")
    tags: list[str] = []
    if isinstance(keywords, str):
        tags = [k.strip() for k in keywords.replace(";", ",").split(",") if k.strip()]
    elif isinstance(keywords, list):
        tags = [str(k).strip() for k in keywords if str(k).strip()]
    return ImportedRef(
        title=title,
        source_file=source_file,
        year=_year(item),
        authors=authors,
        doi=doi,
        arxiv=arxiv,
        kind=kind,
        tags=tags,
        venue=venue or None,
    )
