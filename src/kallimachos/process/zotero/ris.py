"""RIS export from catalog + card citation.

Zotero has no folder import, so one RIS text is produced per category (the channel folder, i.e.
the first folder of the archive path). No network and no dependency on the vault note: the card's
citation completes what the catalog lacks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from kallimachos.interfaces import Card, CatalogEntry, WorkKind
from kallimachos.process.zotero.names import split_name

TY_BY_KIND: dict[WorkKind, str] = {
    WorkKind.ARTICLE: "JOUR",
    WorkKind.PREPRINT: "RPRT",
    WorkKind.BOOK: "BOOK",
    WorkKind.THESIS: "THES",
    WorkKind.CHAPTER: "CHAP",
    WorkKind.UNKNOWN: "GEN",
}
EID_NOTE_PREFIX = "eid:"


def author_tag(name: str) -> str:
    """``Surname, Given`` (RIS ``AU``); the split happens here so Zotero never sees a blob."""
    first, last = split_name(name)
    return f"{last}, {first}" if first else last


def _line(tag: str, value: object | None) -> list[str]:
    if value is None or value == "" or value == []:
        return []
    text = " ".join(str(value).split())  # RIS tags are single-line
    return [f"{tag}  - {text}"]


def category_of(entry: CatalogEntry) -> str:
    if entry.path:
        parts = PurePosixPath(entry.path.replace("\\", "/")).parts
        if len(parts) > 1:
            return parts[0]
    return entry.channel or "uncategorised"


def keyword_of(entry: CatalogEntry) -> str:
    folder = entry.folder
    if not folder and entry.path:
        folder = "/".join(PurePosixPath(entry.path.replace("\\", "/")).parts[:-1])
    return folder or category_of(entry)


def entry_to_ris(entry: CatalogEntry, card: Card | None, *, root: Path | None = None) -> str:
    c = card.citation if card else None
    eid = entry.sha256[:12] if entry.sha256 else (card.eid if card else "")
    title = entry.title or (c.title if c else "")
    authors = list(entry.authors) or (list(c.authors) if c else [])
    year = entry.year if entry.year is not None else (c.year_catalog or c.year if c else None)
    venue = entry.venue or (c.venue if c else None)
    doi = entry.doi or (c.doi if c else None)
    arxiv = entry.arxiv or (c.arxiv if c else None)
    lines = [f"TY  - {TY_BY_KIND.get(entry.kind, 'GEN')}"]
    lines += _line("TI", title)
    for name in authors:
        lines += _line("AU", author_tag(name))
    lines += _line("PY", year)
    lines += _line("JO", venue)
    lines += _line("DO", doi)
    if arxiv:
        lines += _line("UR", f"https://arxiv.org/abs/{arxiv}")
    if c and c.page_range:
        lines += _line("SP", c.page_range)
    if entry.path:
        pdf = Path(root, entry.path) if root else Path(entry.path)
        lines += _line("L1", str(pdf))
    lines += _line("KW", keyword_of(entry))
    lines += _line("N1", f"{EID_NOTE_PREFIX}{eid}" if eid else None)
    lines.append("ER  - ")
    return "\n".join(lines) + "\n"


def catalog_to_ris(
    entries: Sequence[CatalogEntry],
    cards: Mapping[str, Card],
    *,
    only_new: bool = False,
    root: Path | None = None,
) -> dict[str, str]:
    """category -> RIS text. ``TY`` from kind, ``AU`` as ``Surname, Given``, ``L1`` PDF path,
    ``KW <channel>/<subfolder>``, ``N1 eid:<eid>``. ``only_new`` skips entries with ``zotero_key``.
    """
    out: dict[str, list[str]] = {}
    for entry in entries:
        if only_new and entry.zotero_key:
            continue
        eid = entry.sha256[:12] if entry.sha256 else None
        card = cards.get(eid) if eid else None
        out.setdefault(category_of(entry), []).append(entry_to_ris(entry, card, root=root))
    return {cat: "\n".join(records) for cat, records in sorted(out.items())}
