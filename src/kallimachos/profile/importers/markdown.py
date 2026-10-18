"""Markdown notes: frontmatter ``project:``/``tags:``, H1 ``Author (Year) — Title``, DOI/arXiv regex
in the body.

``parse`` accepts a file or a folder (recursive ``*.md``; names starting with ``_`` are index
files and are skipped). The work kind comes from frontmatter ``kind``/``type`` or from tags
(``article``, ``paper``, ``book``, ``thesis``, ``preprint``); otherwise it stays ``unknown`` and
the privacy gate will not query the title.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from kallimachos.interfaces import WorkKind
from kallimachos.profile.importers import ImportedRef, find_arxiv, find_doi
from kallimachos.records.normalize import collapse_whitespace

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_H1_PATTERN = re.compile(
    r"^(?P<authors>.+?)\s*\((?P<year>[^)]*)\)\s*[\u2014\u2013-]\s*(?P<title>.+)$"
)
_YEAR = re.compile(r"(1[6-9]\d\d|20\d\d)")
_KIND_WORDS: dict[str, WorkKind] = {
    "article": WorkKind.ARTICLE,
    "paper": WorkKind.ARTICLE,
    "journal": WorkKind.ARTICLE,
    "book": WorkKind.BOOK,
    "monograph": WorkKind.BOOK,
    "thesis": WorkKind.THESIS,
    "dissertation": WorkKind.THESIS,
    "preprint": WorkKind.PREPRINT,
    "chapter": WorkKind.CHAPTER,
}


def parse(path: Path) -> list[ImportedRef]:
    """Parse one file or folder; never raises on malformed entries, skips them."""
    if path.is_dir():
        files = sorted(p for p in path.rglob("*.md") if not p.name.startswith("_"))
    else:
        files = [path]
    out: list[ImportedRef] = []
    for file in files:
        try:
            ref = parse_note(file)
        except Exception:  # a malformed note must never stop the import
            ref = None
        if ref is not None:
            out.append(ref)
    return out


def parse_note(path: Path) -> ImportedRef | None:
    """One note -> one reference (or ``None`` when the note has no usable title)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    front: dict[str, Any] = {}
    body = text
    fm = _FRONTMATTER.match(text)
    if fm:
        try:
            loaded = yaml.safe_load(fm.group(1))
        except yaml.YAMLError:
            loaded = None
        if isinstance(loaded, dict):
            front = loaded
        body = text[fm.end() :]
    tags = _tags(front)
    project = front.get("project")
    project_str = collapse_whitespace(str(project)) if project else None

    authors: list[str] = []
    year: int | None = None
    title = ""
    h1 = _H1.search(body)
    if h1:
        heading = collapse_whitespace(h1.group(1))
        m = _H1_PATTERN.match(heading)
        if m:
            authors = _split_authors(m.group("authors"))
            ym = _YEAR.search(m.group("year"))
            year = int(ym.group(1)) if ym else None
            title = m.group("title")
        else:
            title = heading
    if not title and front.get("title"):
        title = collapse_whitespace(str(front["title"]))
    if not title:
        return None
    if not authors and front.get("authors"):
        raw = front["authors"]
        authors = (
            [collapse_whitespace(str(a)) for a in raw]
            if isinstance(raw, list)
            else _split_authors(str(raw))
        )
    if not authors and front.get("author"):
        authors = _split_authors(str(front["author"]))
    if year is None and front.get("year"):
        ym = _YEAR.search(str(front["year"]))
        year = int(ym.group(1)) if ym else None
    if year is None:
        ym = re.search(r"(?:^|-)(1[6-9]\d\d|20\d\d)(?:-|$)", path.stem)
        year = int(ym.group(1)) if ym else None
    if not authors:
        first = path.stem.split("-")[0]
        if first and first[0].isupper() and not first.isdigit() and first.lower() != "not":
            authors = [first]

    doi = find_doi(str(front.get("doi") or "")) or find_doi(body)
    arxiv = find_arxiv(str(front.get("arxiv") or "")) or find_arxiv(body)
    kind = _kind(front, tags)
    if kind is WorkKind.UNKNOWN and arxiv:
        kind = WorkKind.PREPRINT
    venue = front.get("venue") or front.get("journal")
    return ImportedRef(
        title=collapse_whitespace(title.replace("$", "")),
        source_file=str(path),
        year=year,
        authors=authors,
        doi=doi,
        arxiv=arxiv,
        kind=kind,
        tags=tags,
        project=project_str,
        venue=collapse_whitespace(str(venue)) if venue else None,
    )


def _tags(front: dict[str, Any]) -> list[str]:
    raw = front.get("tags")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [t.strip().lstrip("#") for t in re.split(r"[,\s]+", raw) if t.strip()]
    if isinstance(raw, list):
        return [str(t).strip().lstrip("#") for t in raw if str(t).strip()]
    return []


def _kind(front: dict[str, Any], tags: list[str]) -> WorkKind:
    for key in ("kind", "type", "work_kind"):
        value = front.get(key)
        if isinstance(value, str):
            word = value.strip().lower()
            if word in _KIND_WORDS:
                return _KIND_WORDS[word]
            try:
                return WorkKind(word)
            except ValueError:
                pass
    for tag in tags:
        word = tag.lower()
        if word in _KIND_WORDS:
            return _KIND_WORDS[word]
    return WorkKind.UNKNOWN


def _split_authors(value: str) -> list[str]:
    parts = re.split(r"\s*(?:,|;|&| and |\+)\s*", value)
    return [collapse_whitespace(p) for p in parts if p.strip()]
