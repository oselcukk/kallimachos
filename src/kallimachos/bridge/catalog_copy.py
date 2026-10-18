"""Catalog copies on both sides of the bridge.

* The computer keeps a *copy* of the server catalog. :func:`update_copy` replaces it only when every
  row of the old copy survives in the new one; otherwise it reports ``CATALOG_DIVERGED`` and leaves
  the copy alone (the old Mac bridge then stayed stale forever, because nothing reconciled it).
* :func:`merge` is the reconcile step: both sides are joined by ``id``; events are unioned, the
  newer row wins field by field (missing fields are filled from the other row), and every row that
  changed gets one ``merged`` event. Two rows with the same id but different ``sha256`` are a real
  conflict and stop the merge (:class:`BridgeConflict`), nothing is written.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kallimachos.archive.catalog import Catalog
from kallimachos.bridge.base import BridgeConflict
from kallimachos.interfaces import CatalogEntry, CatalogEvent, CatalogEventType, dump

_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.UTC)


def load_entries(path: Path) -> list[CatalogEntry]:
    """Catalog rows through ``archive.catalog.Catalog`` (strict build; missing file = empty)."""
    return Catalog.load(path).entries()


def save_entries(path: Path, entries: Iterable[CatalogEntry]) -> None:
    """Validated atomic rewrite through ``archive.catalog.Catalog.save``."""
    Catalog.from_entries(path, entries).save()


def missing_ids(old: Iterable[CatalogEntry], new: Iterable[CatalogEntry]) -> list[str]:
    """Ids of ``old`` that do not survive in ``new``, in ``old`` order: the row is gone, or it now
    names a different file (both sides have a sha256 and they differ)."""
    new_sha = {e.id: e.sha256 for e in new}
    lost: list[str] = []
    for e in old:
        gone = e.id not in new_sha
        if gone or (e.sha256 and new_sha[e.id] and e.sha256 != new_sha[e.id]):
            lost.append(e.id)
    return lost


@dataclass
class CopyResult:
    updated: bool = False
    diverged: bool = False
    missing: list[str] = field(default_factory=list)


def update_copy(local: Path, remote_copy: Path, *, dry_run: bool = False) -> CopyResult:
    """Replace ``local`` with ``remote_copy`` only if no old row would be lost."""
    if not remote_copy.exists():
        return CopyResult()
    new = load_entries(remote_copy)
    old = load_entries(local)
    lost = missing_ids(old, new)
    if lost:
        return CopyResult(diverged=True, missing=lost)
    if [dump(e) for e in old] == [dump(e) for e in new]:
        return CopyResult()
    if not dry_run:
        save_entries(local, new)
    return CopyResult(updated=True)


def _event_key(ev: CatalogEvent) -> tuple[str, str, str]:
    return (ev.at.isoformat(), ev.type.value, json.dumps(dump(ev.detail), sort_keys=True))


def _aware(moment: _dt.datetime) -> _dt.datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=_dt.UTC)


def _last_at(entry: CatalogEntry) -> _dt.datetime:
    return max((_aware(e.at) for e in entry.events), default=_EPOCH)


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def merge_rows(
    mine: CatalogEntry, theirs: CatalogEntry, *, prefer_theirs_on_tie: bool = True
) -> CatalogEntry:
    """One row from two copies of the same work. Raises :class:`BridgeConflict` on sha clash."""
    if mine.sha256 and theirs.sha256 and mine.sha256 != theirs.sha256:
        raise BridgeConflict(
            f"CONFLICT: catalog id {mine.id} has different sha256 on the two sides"
        )
    mine_at, theirs_at = _last_at(mine), _last_at(theirs)
    take_theirs = theirs_at > mine_at or (theirs_at == mine_at and prefer_theirs_on_tie)
    base, other = (theirs, mine) if take_theirs else (mine, theirs)
    merged = dataclasses.replace(base)
    for f in dataclasses.fields(CatalogEntry):
        if f.name in ("events", "keys", "triggers"):
            continue
        if _empty(getattr(merged, f.name)) and not _empty(getattr(other, f.name)):
            setattr(merged, f.name, getattr(other, f.name))
    merged.keys = list(dict.fromkeys([*base.keys, *other.keys]))
    merged.triggers = list(dict.fromkeys([*base.triggers, *other.triggers]))
    events: dict[tuple[str, str, str], CatalogEvent] = {}
    for ev in [*mine.events, *theirs.events]:
        events.setdefault(_event_key(ev), ev)
    merged.events = sorted(events.values(), key=lambda ev: _aware(ev.at))
    return merged


@dataclass
class MergeResult:
    entries: list[CatalogEntry]
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    only_mine: list[str] = field(default_factory=list)


def merge(
    mine: list[CatalogEntry],
    theirs: list[CatalogEntry],
    *,
    source: str,
    now: _dt.datetime,
    prefer_theirs_on_tie: bool = True,
) -> MergeResult:
    """Join ``theirs`` into ``mine`` by id (order: ``mine`` first, then new rows of ``theirs``).
    Every added or changed row gets ``events: merged`` with ``{"source": source}``."""
    theirs_by_id = {e.id: e for e in theirs}
    mine_ids = {e.id for e in mine}
    result = MergeResult(entries=[])
    for row in mine:
        other = theirs_by_id.get(row.id)
        if other is None:
            result.only_mine.append(row.id)
            result.entries.append(row)
            continue
        merged = merge_rows(row, other, prefer_theirs_on_tie=prefer_theirs_on_tie)
        if dump(merged) != dump(row):
            merged.events.append(_merged_event(now, source))
            result.changed.append(row.id)
            result.entries.append(merged)
        else:
            result.entries.append(row)
    for row in theirs:
        if row.id in mine_ids:
            continue
        added = dataclasses.replace(row, events=[*row.events, _merged_event(now, source)])
        result.entries.append(added)
        result.added.append(row.id)
        mine_ids.add(row.id)
    return result


def _merged_event(now: _dt.datetime, source: str) -> CatalogEvent:
    return CatalogEvent(at=now, type=CatalogEventType.MERGED, detail={"source": source})
