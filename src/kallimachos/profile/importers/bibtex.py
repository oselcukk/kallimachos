"""BibTeX entries: ``@type{key, author/title/year/doi/eprint}``.

A small brace-balanced parser (no third-party dependency). ``@comment``, ``@string`` and
``@preamble`` are ignored; entries without a title are skipped.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

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
    "article": WorkKind.ARTICLE,
    "inproceedings": WorkKind.ARTICLE,
    "conference": WorkKind.ARTICLE,
    "proceedings": WorkKind.ARTICLE,
    "book": WorkKind.BOOK,
    "booklet": WorkKind.BOOK,
    "manual": WorkKind.BOOK,
    "phdthesis": WorkKind.THESIS,
    "mastersthesis": WorkKind.THESIS,
    "thesis": WorkKind.THESIS,
    "inbook": WorkKind.CHAPTER,
    "incollection": WorkKind.CHAPTER,
}
_SKIP_TYPES = {"comment", "string", "preamble"}
_ENTRY_START = re.compile(r"@([A-Za-z]+)\s*[{(]")
_FIELD_NAME = re.compile(r"\s*([A-Za-z][A-Za-z0-9_\-:]*)\s*=\s*")
_LATEX_ACCENT = re.compile(r"\\[`'^\"~=.uvHtcdbkr]\s*\{?([A-Za-z])\}?")
_LATEX_CMD = re.compile(r"\\[A-Za-z]+\s*")
_YEAR = re.compile(r"(1[6-9]\d\d|20\d\d)")


def parse(path: Path) -> list[ImportedRef]:
    """Parse one file; never raises on malformed entries, skips them."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[ImportedRef] = []
    for entry_type, body in _entries(text):
        try:
            ref = _ref_from_entry(entry_type, body, str(path))
        except Exception:  # a malformed entry must never stop the import
            ref = None
        if ref is not None:
            out.append(ref)
    return out


def _entries(text: str) -> Iterator[tuple[str, str]]:
    """Yield ``(type, body)`` for every ``@type{...}`` block, brace balanced."""
    pos = 0
    while True:
        m = _ENTRY_START.search(text, pos)
        if not m:
            return
        open_ch = text[m.end() - 1]
        close_ch = "}" if open_ch == "{" else ")"
        depth = 1
        i = m.end()
        while i < len(text) and depth:
            ch = text[i]
            if ch == "{" or (ch == open_ch and open_ch == "("):
                depth += 1
            elif ch == "}" or (ch == close_ch and close_ch == ")"):
                depth -= 1
            i += 1
        if depth:
            return  # unterminated entry: nothing reliable follows
        body = text[m.end() : i - 1]
        pos = i
        entry_type = m.group(1).lower()
        if entry_type in _SKIP_TYPES:
            continue
        yield entry_type, body


def _fields(body: str) -> dict[str, str]:
    """Parse ``key, name = {value}, name = "value", name = 123`` into a mapping."""
    comma = body.find(",")
    if comma < 0:
        return {}
    rest = body[comma + 1 :]
    fields: dict[str, str] = {}
    i = 0
    while i < len(rest):
        m = _FIELD_NAME.match(rest, i)
        if not m:
            break
        name = m.group(1).lower()
        i = m.end()
        value, i = _value(rest, i)
        fields[name] = value
        # skip to the next comma
        while i < len(rest) and rest[i] != ",":
            i += 1
        i += 1
    return fields


def _value(text: str, i: int) -> tuple[str, int]:
    parts: list[str] = []
    while i < len(text):
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        if i >= len(text):
            break
        ch = text[i]
        if ch == "{":
            depth, j = 1, i + 1
            while j < len(text) and depth:
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                j += 1
            parts.append(text[i + 1 : j - 1])
            i = j
        elif ch == '"':
            j = i + 1
            depth = 0
            while j < len(text):
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                elif text[j] == '"' and depth == 0:
                    break
                j += 1
            parts.append(text[i + 1 : j])
            i = j + 1
        elif ch == ",":
            break
        else:
            j = i
            while j < len(text) and text[j] not in ",#\r\n":
                j += 1
            parts.append(text[i:j].strip())
            i = j
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        if i < len(text) and text[i] == "#":
            i += 1
            continue
        break
    return "".join(parts), i


def clean_latex(value: str) -> str:
    """Remove braces and the common LaTeX accent/command noise; keep the letters."""
    out = _LATEX_ACCENT.sub(r"\1", value)
    out = out.replace("--", "-").replace("~", " ")
    out = _LATEX_CMD.sub("", out)
    out = out.replace("{", "").replace("}", "").replace("\\", "")
    return collapse_whitespace(out)


def split_authors(value: str) -> list[str]:
    """``A and B and C`` -> ``[A, B, C]`` with ``Last, First`` kept as written."""
    cleaned = clean_latex(value)
    names = re.split(r"\s+and\s+", cleaned, flags=re.IGNORECASE)
    return [n.strip() for n in names if n.strip() and n.strip().lower() != "others"]


def _ref_from_entry(entry_type: str, body: str, source_file: str) -> ImportedRef | None:
    fields = _fields(body)
    title = clean_latex(fields.get("title", ""))
    if not title:
        return None
    year_m = _YEAR.search(fields.get("year", "") or fields.get("date", ""))
    year = int(year_m.group(1)) if year_m else None
    doi = clean_doi(fields["doi"]) if fields.get("doi") else None
    if doi is None and fields.get("url"):
        doi = find_doi(fields["url"])
    arxiv: str | None = None
    eprint = fields.get("eprint")
    archive_prefix = (fields.get("archiveprefix") or fields.get("primaryclass") or "").lower()
    if eprint and ("arxiv" in archive_prefix or not archive_prefix):
        arxiv = normalize_arxiv(eprint)
    if arxiv is None:
        for key in ("note", "url", "journal", "howpublished"):
            if fields.get(key):
                arxiv = find_arxiv(fields[key])
                if arxiv:
                    break
    kind = _KIND.get(entry_type, WorkKind.UNKNOWN)
    if kind is WorkKind.UNKNOWN and arxiv:
        kind = WorkKind.PREPRINT
    venue = clean_latex(fields.get("journal") or fields.get("booktitle") or "")
    tags = [t.strip() for t in re.split(r"[,;]", fields.get("keywords", "")) if t.strip()]
    return ImportedRef(
        title=title,
        source_file=source_file,
        year=year,
        authors=split_authors(fields.get("author", "")),
        doi=doi,
        arxiv=arxiv,
        kind=kind,
        tags=tags,
        venue=venue or None,
    )
