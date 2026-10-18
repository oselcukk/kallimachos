"""``process/notes_index.json``: eid -> note path.

Also home of the small frontmatter helpers (``split_frontmatter``, ``parse_frontmatter``,
``note_title``) that ``note``, ``links`` and ``cluster`` share; they live here because this module
has no dependency on the others.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from kallimachos.interfaces import EID_PATTERN, SchemaViolation
from kallimachos.records.normalize import match_key
from kallimachos.state import store

EID_RE = re.compile(EID_PATTERN)
_FM_EID_RE = re.compile(r'^eid:\s*"?([0-9a-f]{12})"?\s*$', re.MULTILINE)
_YEAR_RE = re.compile(r"^(1[5-9]|20)[0-9]{2}$")
SYSTEM_FILE_PREFIX = "_"
"""Files whose name starts with ``_`` are Kallimachos/system files, never literature notes."""


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Return ``(frontmatter_text, body)``. ``frontmatter_text`` is the raw YAML between the two
    ``---`` lines (``None`` when the file has no frontmatter)."""
    if not text.startswith("---"):
        return None, text
    first_end = text.find("\n")
    if first_end == -1 or text[:first_end].strip() != "---":
        return None, text
    match = re.search(r"^(---|\.\.\.)[ \t]*$", text[first_end + 1 :], re.MULTILINE)
    if match is None:
        return None, text
    fm = text[first_end + 1 : first_end + 1 + match.start()]
    body = text[first_end + 1 + match.end() :]
    if body.startswith("\n"):
        body = body[1:]
    return fm, body


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse the YAML frontmatter into a mapping (empty when absent or not a mapping)."""
    fm, body = split_frontmatter(text)
    if fm is None:
        return {}, body
    try:
        data = yaml.safe_load(fm)
    except yaml.YAMLError:
        return {}, body
    return (dict(data) if isinstance(data, dict) else {}), body


def note_title(text: str, fallback: str) -> str:
    """First H1 of the body (without ``# ``), else ``fallback``."""
    _fm, body = split_frontmatter(text)
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def eid_of(text: str) -> str | None:
    """The ``eid:`` frontmatter value (the primary note <-> work key), if any."""
    fm, _body = split_frontmatter(text)
    if fm is None:
        return None
    match = _FM_EID_RE.search(fm)
    return match.group(1) if match else None


def iter_notes(folder: Path) -> list[Path]:
    """Every ``*.md`` under ``folder`` (recursive) that is not a ``_``-prefixed system file."""
    if not folder.is_dir():
        return []
    return sorted(
        p
        for p in folder.rglob("*.md")
        if p.is_file()
        and not p.name.startswith(SYSTEM_FILE_PREFIX)
        and not any(part.startswith(".") for part in p.relative_to(folder).parts)
    )


@dataclass
class NoteRef:
    path: str  # relative to the vault / plain folder root
    title: str
    updated_at: str
    zotero_key: str | None = None
    citekey: str | None = None


class NotesIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, NoteRef] = {}

    @classmethod
    def load(cls, path: Path) -> NotesIndex:
        index = cls(path)
        data = store.read_json(path, default={})
        if not isinstance(data, dict):
            raise SchemaViolation("notes_index.json must be an object", str(path))
        for eid, raw in data.items():
            if not isinstance(raw, dict) or not EID_RE.match(str(eid)):
                raise SchemaViolation("invalid notes_index entry", f"{path}:{eid}")
            index._entries[str(eid)] = NoteRef(
                path=str(raw.get("path", "")),
                title=str(raw.get("title", "")),
                updated_at=str(raw.get("updated_at", "")),
                zotero_key=raw.get("zotero_key"),
                citekey=raw.get("citekey"),
            )
        return index

    def __len__(self) -> int:
        return len(self._entries)

    def items(self) -> list[tuple[str, NoteRef]]:
        return sorted(self._entries.items())

    def get(self, eid: str) -> NoteRef | None:
        return self._entries.get(eid)

    def set(self, eid: str, ref: NoteRef) -> None:
        if not EID_RE.match(eid):
            raise SchemaViolation("eid must be 12 hex characters", "eid")
        self._entries[eid] = ref

    def by_path(self, rel_path: str) -> str | None:
        """eid whose note lives at ``rel_path`` (POSIX, relative to the root), if any."""
        for eid, ref in self._entries.items():
            if ref.path == rel_path:
                return eid
        return None

    def find(self, surname: str, year: int | None = None) -> list[tuple[str, NoteRef]]:
        """``process notes-index find <surname> [year]``.

        Matches the surname segments of the file name (``<Surname>-<Year>-<Short-Title>.md``, the
        ``etal`` form included) or a word of the title; ``year`` filters on the name's year.
        """
        wanted = match_key(surname)
        if not wanted:
            return []
        out: list[tuple[str, NoteRef]] = []
        for eid, ref in self.items():
            stem = Path(ref.path).stem
            parts = stem.split("-")
            year_idx = next((i for i, p in enumerate(parts) if _YEAR_RE.match(p)), None)
            name_parts = parts[:year_idx] if year_idx is not None else parts
            note_year = int(parts[year_idx]) if year_idx is not None else None
            if year is not None and note_year is not None and note_year != year:
                continue
            if year is not None and note_year is None:
                continue
            keys = {match_key(p) for p in name_parts}
            if wanted in keys or wanted in match_key(ref.title).split():
                out.append((eid, ref))
        return out

    def rebuild(self, literature_folder: Path, root: Path) -> int:
        """Scan ``eid:`` frontmatter recursively; return the count."""
        self._entries = {}
        for path in iter_notes(literature_folder):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            eid = eid_of(text)
            if eid is None:
                continue
            fm, _body = parse_frontmatter(text)
            try:
                rel = path.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                rel = path.as_posix()
            updated = fm.get("updated_at") or fm.get("read_at")
            if isinstance(updated, _dt.date):
                updated_s = updated.isoformat()
            elif updated:
                updated_s = str(updated)
            else:
                updated_s = _dt.datetime.fromtimestamp(path.stat().st_mtime, tz=_dt.UTC).strftime(
                    "%Y-%m-%d"
                )
            zotero = fm.get("zotero")
            zotero_key = None
            if isinstance(zotero, str) and zotero:
                zotero_key = zotero.rsplit("/", 1)[-1]
            citekey = fm.get("citekey")
            self._entries[eid] = NoteRef(
                path=rel,
                title=note_title(text, path.stem),
                updated_at=updated_s,
                zotero_key=zotero_key,
                citekey=str(citekey) if citekey else None,
            )
        return len(self._entries)

    def save(self) -> None:
        data = {eid: _compact(asdict(ref)) for eid, ref in self.items()}
        store.write_json_atomic(self.path, data)


def _compact(raw: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in raw.items() if v is not None}
