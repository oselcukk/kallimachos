"""Reading queue (``process queue``).

Avenox installed: ``beyin.py task-create --file`` (id ``kallimachos-read-<id>``, not recreated);
otherwise a line in ``<literature_folder>/_Reading-Queue.md`` inside the marked block.

``reading_queue.jsonl`` is append-only like the other state files: the last line per
``(ident, kind)`` wins, so a sync appends the entry again with ``synced`` filled.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import tempfile
from pathlib import Path
from typing import Any, Literal

from kallimachos.config.schema import Config
from kallimachos.i18n import load_catalog
from kallimachos.interfaces import (
    PartialFailure,
    QueueSync,
    ReadingQueueEntry,
    UsageError,
    build,
    dump,
    json_schema,
)
from kallimachos.paths import DataLayout
from kallimachos.process import avenox
from kallimachos.process import note as note_mod
from kallimachos.process.notes_index import EID_RE
from kallimachos.state import store

QUEUE_BLOCK_KIND = "reading_queue"
TASK_ID_PREFIX = "kallimachos-read-"
TASK_ID_MAX = 100  # Avenox: [a-zA-Z0-9][a-zA-Z0-9_-]{0,99}
_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def ident_of(entry: ReadingQueueEntry) -> str:
    return entry.eid or entry.candidate_id or ""


def task_id_for(ident: str) -> str:
    """``kallimachos-read-<slug>``: lower-case, ``[a-z0-9_-]`` only, at most 100 characters."""
    slug = _SLUG_RE.sub("-", ident.strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        raise UsageError(f"cannot build a task id from {ident!r}")
    return (TASK_ID_PREFIX + slug)[:TASK_ID_MAX].rstrip("-")


def _schema() -> dict[str, Any]:
    return json_schema(ReadingQueueEntry)


def load_entries(layout: DataLayout) -> list[ReadingQueueEntry]:
    return [build(ReadingQueueEntry, raw) for raw in store.read_jsonl(layout.reading_queue_file)]


def current_entries(layout: DataLayout) -> dict[tuple[str, str], ReadingQueueEntry]:
    """Latest line per ``(ident, kind)``."""
    out: dict[tuple[str, str], ReadingQueueEntry] = {}
    for entry in load_entries(layout):
        out[(ident_of(entry), entry.kind)] = entry
    return out


def _append(layout: DataLayout, entry: ReadingQueueEntry) -> None:
    store.append_jsonl(layout.reading_queue_file, dump(entry), schema=_schema())


def queue_add(
    layout: DataLayout,
    *,
    ident: str,
    kind: Literal["read", "ask"],
    title: str,
    url: str | None,
    note: str | None,
) -> ReadingQueueEntry:
    """Append one entry; an identical open ``(ident, kind)`` is returned, not duplicated."""
    ident = ident.strip()
    if not ident:
        raise UsageError("an eid or candidate id is required")
    existing = current_entries(layout).get((ident, kind))
    if existing is not None:
        return existing
    is_eid = EID_RE.match(ident) is not None
    entry = ReadingQueueEntry(
        at=_dt.datetime.now(tz=_dt.UTC),
        kind=kind,
        title=title,
        candidate_id=None if is_eid else ident,
        eid=ident if is_eid else None,
        url=url,
        note=note,
        synced=None,
    )
    _append(layout, entry)
    return entry


def task_payload(entry: ReadingQueueEntry, config: Config) -> dict[str, Any]:
    """``task-create --file`` JSON: body text plus the documented metadata."""
    owner = config.notes.task_owner
    if not owner:
        raise UsageError(
            "notes.task_owner is not set; Avenox task-create needs an explicit owner "
            "(the user's preferred form of address)"
        )
    ident = ident_of(entry)
    task_id = task_id_for(ident)
    title = entry.title or ident
    lines = [f"{entry.kind}: {title}"]
    if entry.url:
        lines.append(entry.url)
    if entry.note:
        lines.append(entry.note)
    metadata: dict[str, Any] = {
        "id": task_id,
        "title": title,
        "kind": "task",
        "revision": 1,
        "status": "inbox",
        "owner": owner,
        "visibility": "internal",
        "facts": {"candidate_id": ident, "url": entry.url, "kind": entry.kind},
    }
    project = config.notes.frontmatter.project
    if project:
        metadata["project"] = project
    return {"source": f"tasks/{task_id}.md", "text": "\n".join(lines), "metadata": metadata}


def _with_sync(entry: ReadingQueueEntry, synced: QueueSync) -> ReadingQueueEntry:
    data = dump(entry)
    data["synced"] = dump(synced)
    return build(ReadingQueueEntry, data)


def _sync_avenox(
    layout: DataLayout,
    config: Config,
    info: avenox.AvenoxInfo,
    pending: list[ReadingQueueEntry],
) -> tuple[list[ReadingQueueEntry], list[str]]:
    synced: list[ReadingQueueEntry] = []
    errors: list[str] = []
    for entry in pending:
        payload = task_payload(entry, config)
        task_id = str(payload["metadata"]["id"])
        if avenox.task_exists(info, task_id):
            # never recreate an id: adopt the existing task
            updated = _with_sync(entry, QueueSync(avenox_task_id=task_id))
            _append(layout, updated)
            synced.append(updated)
            continue
        with tempfile.TemporaryDirectory(prefix="kallimachos-task-") as tmp:
            task_file = Path(tmp) / f"{task_id}.json"
            task_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            result = avenox.task_create(info, task_file)
        if result.get("status") == "error":
            message = str(result.get("message", ""))
            if "exists" in message:
                updated = _with_sync(entry, QueueSync(avenox_task_id=task_id))
                _append(layout, updated)
                synced.append(updated)
                continue
            errors.append(f"{task_id}: {result.get('error')}: {message}")
            continue
        returned_id = str(result.get("id", ""))
        expected_source = payload["source"]
        if returned_id != task_id or result.get("source") != expected_source:
            errors.append(
                f"{task_id}: Avenox returned id={returned_id!r} source={result.get('source')!r}"
            )
            continue
        updated = _with_sync(entry, QueueSync(avenox_task_id=returned_id))
        _append(layout, updated)
        synced.append(updated)
    return synced, errors


def queue_file(config: Config) -> Path:
    return note_mod.literature_root(config) / config.notes.reading_queue_file


def _sync_plain(
    layout: DataLayout, config: Config, pending: list[ReadingQueueEntry]
) -> list[ReadingQueueEntry]:
    path = queue_file(config)
    lang = config.notes.language or config.language
    line_fmt = load_catalog(lang)["queue.line"]
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if text and not note_mod.managed_block_re(QUEUE_BLOCK_KIND).search(text):
        raise UsageError(
            f"{path} exists without a Kallimachos reading-queue block; add the block markers or "
            "change notes.reading_queue_file"
        )
    match = note_mod.managed_block_re(QUEUE_BLOCK_KIND).search(text)
    existing_lines: list[str] = []
    if match:
        inner = match.group(0).split("\n")[1:-1]
        existing_lines = [ln for ln in inner if ln.strip()]
    new_lines = list(existing_lines)
    for entry in pending:
        line = line_fmt.format(
            title=entry.title or ident_of(entry),
            url=entry.url or ident_of(entry),
            kind=entry.kind,
            date=entry.at.date().isoformat(),
        )
        if line not in new_lines:
            new_lines.append(line)
    if not text:
        text = "# Reading queue\n"
    updated = note_mod.upsert_managed_block(
        text, kind=QUEUE_BLOCK_KIND, block="\n".join(new_lines), attrs={}
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    store.write_text_atomic(path, updated)
    file_lines = updated.split("\n")
    synced: list[ReadingQueueEntry] = []
    for entry in pending:
        line = line_fmt.format(
            title=entry.title or ident_of(entry),
            url=entry.url or ident_of(entry),
            kind=entry.kind,
            date=entry.at.date().isoformat(),
        )
        line_no = file_lines.index(line) + 1 if line in file_lines else None
        updated_entry = _with_sync(entry, QueueSync(queue_file_line=line_no))
        _append(layout, updated_entry)
        synced.append(updated_entry)
    return synced


def queue_sync(layout: DataLayout, config: Config) -> list[ReadingQueueEntry]:
    """Deliver unsynced entries via Avenox task-create or the queue file; return synced entries.
    A partially failed Avenox delivery raises ``PartialFailure`` after the successful ones are
    recorded."""
    pending = [e for e in current_entries(layout).values() if e.synced is None]
    pending.sort(key=lambda e: e.at)
    if not pending:
        return []
    root = note_mod.vault_root(config)
    info = avenox.detect(root) if config.notes.target == "avenox" else None
    if info is not None:
        synced, errors = _sync_avenox(layout, config, info, pending)
        if errors:
            raise PartialFailure("Avenox task-create failed for: " + "; ".join(errors))
        return synced
    return _sync_plain(layout, config, pending)
