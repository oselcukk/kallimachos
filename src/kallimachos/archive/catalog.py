"""``archive/catalog.jsonl`` access.

The catalog keeps one row per work. Every change after creation is an entry in ``events[]``
(the single change log); ``status`` is the current state. Rows are never deleted.
``unreachable.jsonl`` is handled by :class:`UnreachableList`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from kallimachos.archive.identity import entry_keys, normalize_key
from kallimachos.archive.rows import read_rows, write_rows
from kallimachos.interfaces import CatalogEntry, CatalogEvent, UnreachableEntry


class Catalog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: list[CatalogEntry] = []

    @classmethod
    def load(cls, path: Path) -> Catalog:
        """Strictly built rows; a missing file is an empty catalog."""
        cat = cls(path)
        cat._entries = read_rows(path, CatalogEntry)
        return cat

    @classmethod
    def from_entries(cls, path: Path, entries: Iterable[CatalogEntry]) -> Catalog:
        cat = cls(path)
        cat.replace(entries)
        return cat

    def replace(self, entries: Iterable[CatalogEntry]) -> None:
        """Replace every row in memory (``save`` writes them); ids must be unique."""
        rows = list(entries)
        ids = [e.id for e in rows]
        if len(set(ids)) != len(ids):
            dup = next(i for i in ids if ids.count(i) > 1)
            raise ValueError(f"catalog has two entries with id {dup}")
        self._entries = rows

    def entries(self) -> list[CatalogEntry]:
        return list(self._entries)

    def all_keys(self) -> set[str]:
        out: set[str] = set()
        for entry in self._entries:
            out.update(entry_keys(entry))
        return out

    def find_by_keys(self, keys: Sequence[str]) -> CatalogEntry | None:
        wanted = {normalize_key(k) for k in keys}
        if not wanted:
            return None
        for entry in self._entries:
            if wanted.intersection(entry_keys(entry)):
                return entry
        return None

    def find_by_sha(self, sha256: str) -> CatalogEntry | None:
        for entry in self._entries:
            if entry.sha256 and entry.sha256 == sha256:
                return entry
        return None

    def get(self, entry_id: str) -> CatalogEntry | None:
        for entry in self._entries:
            if entry.id == entry_id:
                return entry
        return None

    def add(self, entry: CatalogEntry) -> None:
        if self.get(entry.id) is not None:
            raise ValueError(f"catalog already has an entry with id {entry.id}")
        self._entries.append(entry)

    def add_event(self, entry_id: str, event: CatalogEvent) -> None:
        entry = self.get(entry_id)
        if entry is None:
            raise KeyError(entry_id)
        entry.events.append(event)

    def save(self) -> None:
        """Rewrite atomically through ``state.store``; validated against ``catalog.schema.json``."""
        write_rows(self.path, self._entries)


def eid_from_sha(sha256: str) -> str:
    """``sha256[:12]``."""
    return sha256[:12]


class UnreachableList:
    """``archive/unreachable.jsonl``."""

    def __init__(self, path: Path, entries: list[UnreachableEntry] | None = None) -> None:
        self.path = path
        self._entries = entries or []

    @classmethod
    def load(cls, path: Path) -> UnreachableList:
        return cls(path, read_rows(path, UnreachableEntry))

    def entries(self) -> list[UnreachableEntry]:
        return list(self._entries)

    def open_entries(self) -> list[UnreachableEntry]:
        return [e for e in self._entries if e.status == "open"]

    def open_keys(self) -> set[str]:
        out: set[str] = set()
        for e in self.open_entries():
            out.update(normalize_key(k) for k in e.keys)
        return out

    def find(self, keys: Sequence[str]) -> UnreachableEntry | None:
        wanted = {normalize_key(k) for k in keys}
        for e in self._entries:
            if wanted.intersection(normalize_key(k) for k in e.keys):
                return e
        return None

    def upsert(self, entry: UnreachableEntry) -> None:
        old = self.find(entry.keys)
        if old is None:
            self._entries.append(entry)
            return
        old.reason = entry.reason
        old.oa_tried = entry.oa_tried
        old.landing = entry.landing or old.landing
        old.folder = entry.folder or old.folder
        old.filename = entry.filename or old.filename
        if old.status != "dropped":
            old.status = "open"
            old.closed_by = None

    def close(self, keys: Sequence[str], closed_by: str) -> bool:
        entry = self.find(keys)
        if entry is None or entry.status != "open":
            return False
        entry.status = "closed"
        entry.closed_by = closed_by
        return True

    def save(self) -> None:
        write_rows(self.path, self._entries)
