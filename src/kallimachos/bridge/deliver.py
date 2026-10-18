"""pull / deliver / push-ack / reconcile / _Incoming.

Permanent rules: no delete, no overwrite, never touch unmarked user files, stop on conflict.

Round on the computer (``bridge pull`` -> ``bridge deliver`` -> ``bridge push-ack``):

1. **pull**: server ``archive/outgoing/**.pdf``, ``archive/catalog.jsonl`` and
   ``archive/unreachable.jsonl`` -> ``bridge/incoming/``. The local catalog copy is refreshed only
   when every old row survives; otherwise ``CATALOG_DIVERGED`` is recorded in ``bridge/state.json``.
2. **deliver**: each pulled PDF needs a safe folder and name, a catalog row ``status: outgoing``
   with the same folder/name and the same sha256. Target exists: same sha -> delivered, different
   sha -> ``CONFLICT`` (the round stops), cloud placeholder -> next round. New files are written
   with ``O_EXCL`` and verified. Results go to ``bridge/delivered.jsonl``. Then ``_Incoming/``.
3. **push-ack**: ``archive confirm --sha`` on the server for every delivered, unacknowledged file
   (the server re-checks its own copy and moves it ``outgoing/`` -> ``delivered/``); rows created
   on the computer (``user_supplied``) are pushed to the server inbox and merged there.

``bridge reconcile`` joins catalog copies by id with ``events: merged`` on either side: on the
computer it merges the pulled server copy into the local one, on the server it merges the inbox.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kallimachos.archive.naming import SAFE_NAME_RE
from kallimachos.bridge.base import BridgeConflict, Transport, kallimachos_argv
from kallimachos.bridge.catalog_copy import load_entries, merge, save_entries, update_copy
from kallimachos.bridge.files import (
    INCOMING_DIR_NAME,
    copy_verified,
    inside_root,
    is_dataless,
    is_regular_file,
    move_exclusive,
    safe_folder,
    sha256_file,
    write_marked,
)
from kallimachos.bridge.incoming import (
    identities_from_filename,
    identities_from_text,
    keys_intersect,
    pdftotext_first_pages,
    title_matches,
)
from kallimachos.config.schema import Config
from kallimachos.interfaces import (
    CatalogEntry,
    CatalogEvent,
    CatalogEventType,
    CatalogStatus,
    PartialFailure,
    UnreachableEntry,
    UsageError,
    build,
)
from kallimachos.paths import DataLayout
from kallimachos.state import store

__all__ = [
    "BridgeConflict",
    "DeliverReport",
    "confirm",
    "deliver",
    "process_incoming",
    "pull",
    "push_ack",
    "reconcile",
]

REMOTE_OUTGOING = "archive/outgoing/"
REMOTE_CATALOG = "archive/catalog.jsonl"
REMOTE_UNREACHABLE = "archive/unreachable.jsonl"
REMOTE_INBOX = "bridge/inbox"
UNSORTED_FOLDER = "_Unsorted"
REPORT_NAME = "_report.md"

TextReader = Callable[[Path], str]
Namer = Callable[[UnreachableEntry], tuple[str, str]]


# --------------------------------------------------------------------------- locations


def outgoing_dir(layout: DataLayout) -> Path:
    """Server side: ``archive/outgoing/<folder>/<name>.pdf`` waiting for delivery."""
    return layout.archive_outgoing


def delivered_dir(layout: DataLayout) -> Path:
    """Server side: files confirmed by the computer (moved here, never deleted)."""
    return layout.archive_delivered


def inbox_dir(layout: DataLayout) -> Path:
    """Server side: catalog rows pushed by the computer, merged by ``bridge reconcile``."""
    return layout.bridge_inbox


def cache_outgoing(layout: DataLayout) -> Path:
    return layout.bridge_incoming / "outgoing"


def cache_catalog(layout: DataLayout) -> Path:
    return layout.bridge_incoming / "catalog.jsonl"


def cache_unreachable(layout: DataLayout) -> Path:
    return layout.bridge_incoming / "unreachable.jsonl"


def outbox_file(layout: DataLayout) -> Path:
    """Computer side: catalog rows the server has not seen yet (``user_supplied``)."""
    return layout.bridge_outbox_file


def state_file(layout: DataLayout) -> Path:
    return layout.bridge_state_file


def archive_root(config: Config) -> Path:
    raw = config.bridge.local_archive_root or config.archive.root
    if not raw:
        raise UsageError("bridge.local_archive_root or archive.root must be set")
    return Path(raw).expanduser()


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0)


# --------------------------------------------------------------------------- state


def read_state(layout: DataLayout) -> dict[str, Any]:
    path = state_file(layout)
    if not path.exists():
        return {}
    data = store.read_json(path, default={})
    return dict(data) if isinstance(data, dict) else {}


def _update_state(layout: DataLayout, **changes: Any) -> None:
    state = read_state(layout)
    state.update(changes)
    store.write_json_atomic(state_file(layout), state)


def _delivered_lines(layout: DataLayout) -> list[dict[str, Any]]:
    path = layout.bridge_delivered_file
    return list(store.read_jsonl(path)) if path.exists() else []


def delivered_shas(layout: DataLayout) -> set[str]:
    return {str(r["sha256"]) for r in _delivered_lines(layout) if r.get("state") == "delivered"}


def pending_acks(layout: DataLayout) -> list[dict[str, Any]]:
    """Delivered lines without a later ``acked`` line, oldest first, one per sha."""
    acked = {r["sha256"] for r in _delivered_lines(layout) if r.get("state") == "acked"}
    out: dict[str, dict[str, Any]] = {}
    for row in _delivered_lines(layout):
        if row.get("state") == "delivered" and row["sha256"] not in acked:
            out.setdefault(str(row["sha256"]), row)
    return list(out.values())


def _record_delivery(layout: DataLayout, entry: CatalogEntry, rel: str, sha: str) -> None:
    store.append_jsonl(
        layout.bridge_delivered_file,
        {
            "at": _now().isoformat(),
            "id": entry.id,
            "sha256": sha,
            "path": rel,
            "state": "delivered",
        },
    )


# --------------------------------------------------------------------------- pull


def pull(
    transport: Transport, layout: DataLayout, config: Config, *, dry_run: bool = False
) -> list[Path]:
    """``outgoing/*.pdf`` + catalog + unreachable -> ``bridge/incoming/``.

    Returns the PDFs now in the cache. The local catalog copy is updated only when all of its rows
    survive in the server copy; otherwise ``bridge/state.json`` records ``CATALOG_DIVERGED`` with
    the missing ids (see :func:`reconcile`). ``dry_run`` transfers nothing."""
    if dry_run:
        return []
    transport.pull(REMOTE_OUTGOING, cache_outgoing(layout), pattern="*.pdf", missing_ok=True)
    transport.pull(REMOTE_CATALOG, cache_catalog(layout), missing_ok=True)
    transport.pull(REMOTE_UNREACHABLE, cache_unreachable(layout), missing_ok=True)
    copy = update_copy(layout.catalog_file, cache_catalog(layout))
    _update_state(
        layout,
        pulled_at=_now().isoformat(),
        catalog="diverged" if copy.diverged else ("updated" if copy.updated else "unchanged"),
        catalog_missing_ids=copy.missing,
    )
    return cached_pdfs(layout)


def cached_pdfs(layout: DataLayout) -> list[Path]:
    base = cache_outgoing(layout)
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("*.pdf") if not p.name.startswith("."))


# --------------------------------------------------------------------------- deliver


@dataclass
class DeliverReport:
    delivered: list[Path] = field(default_factory=list)
    already_present: list[Path] = field(default_factory=list)
    deferred_dataless: list[Path] = field(default_factory=list)
    moved: list[tuple[Path, Path]] = field(default_factory=list)  # (expected target, found at)
    conflicts: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def merge(self, other: DeliverReport) -> None:
        self.delivered += other.delivered
        self.already_present += other.already_present
        self.deferred_dataless += other.deferred_dataless
        self.moved += other.moved
        self.conflicts += other.conflicts
        self.rejected += other.rejected
        self.notes += other.notes


def _files_by_size(root: Path) -> dict[int, list[Path]]:
    """Regular PDF files under ``root`` grouped by size (symlinks and ``_Incoming`` skipped); the
    size filter keeps the sha check cheap."""
    out: dict[int, list[Path]] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not d.startswith(".") and d != INCOMING_DIR_NAME and not (here / d).is_symlink()
        )
        for name in filenames:
            if not name.lower().endswith(".pdf"):
                continue
            p = here / name
            if is_regular_file(p) and not is_dataless(p):
                out.setdefault(p.stat().st_size, []).append(p)
    return out


def _find_same_content(
    sizes: dict[int, list[Path]], size: int, sha: str, *, exclude: Path
) -> Path | None:
    for candidate in sizes.get(size, []):
        if candidate == exclude:
            continue
        try:
            if sha256_file(candidate) == sha:
                return candidate
        except OSError:
            continue
    return None


def deliver(layout: DataLayout, config: Config, *, dry_run: bool = False) -> DeliverReport:
    """Place verified PDFs from ``bridge/incoming/outgoing`` into the archive root. Stops at the
    first ``CONFLICT``; ``dry_run`` checks everything and writes nothing."""
    root = archive_root(config)
    if not root.is_dir():
        raise UsageError(f"archive root does not exist: {root}")
    report = DeliverReport()
    outgoing = {
        (e.folder or "", e.filename): e
        for e in load_entries(cache_catalog(layout))
        if e.status is CatalogStatus.OUTGOING and e.filename
    }
    done = delivered_shas(layout)
    base = cache_outgoing(layout)
    sizes: dict[int, list[Path]] | None = None  # archive files by size, built on first need
    for path in cached_pdfs(layout):
        rel = path.relative_to(base).as_posix()
        folder, _, name = rel.rpartition("/")
        if not is_regular_file(path):
            report.rejected.append(f"INVALID (symlink or special file): {rel}")
            continue
        if not safe_folder(folder) or not SAFE_NAME_RE.match(name):
            report.rejected.append(f"INVALID (folder or name): {rel}")
            continue
        entry = outgoing.get((folder, name))
        if entry is None:
            report.rejected.append(f"NOT_IN_CATALOG (no outgoing row): {rel}")
            continue
        sha = sha256_file(path)
        if sha != entry.sha256:
            report.rejected.append(f"SHA_MISMATCH (not delivered): {rel}")
            continue
        if sha in done:
            continue  # delivered in an earlier round, waiting for push-ack
        target = root / folder / name if folder else root / name
        if not inside_root(target, root):
            report.rejected.append(f"INVALID (target outside the archive root): {rel}")
            continue
        if target.exists() or target.is_symlink():
            if is_dataless(target):
                report.deferred_dataless.append(target)
                continue
            if is_regular_file(target) and sha256_file(target) == sha:
                report.already_present.append(target)
                if not dry_run:
                    _record_delivery(layout, entry, rel, sha)
                continue
            report.conflicts.append(f"CONFLICT (target exists with different content): {rel}")
            break
        if sizes is None:
            sizes = _files_by_size(root)
        moved = _find_same_content(sizes, path.stat().st_size, sha, exclude=target)
        if moved is not None:
            # the user moved an earlier delivery elsewhere in the archive: never copy it twice
            report.moved.append((target, moved))
            report.notes.append(f"MOVED: {rel} -> {moved.relative_to(root).as_posix()}")
            if not dry_run:
                _record_delivery(layout, entry, moved.relative_to(root).as_posix(), sha)
            continue
        if dry_run:
            report.notes.append(f"would deliver: {rel}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if not inside_root(target, root):
            report.rejected.append(f"INVALID (target outside the archive root): {rel}")
            continue
        try:
            copy_verified(path, target, sha)
        except FileExistsError:
            report.conflicts.append(f"CONFLICT (target appeared during copy): {rel}")
            break
        except OSError as exc:
            report.rejected.append(f"COPY_FAILED ({exc.strerror or exc}): {rel}")
            continue
        _record_delivery(layout, entry, rel, sha)
        report.delivered.append(target)
    if not dry_run:
        _update_state(
            layout,
            delivered_at=_now().isoformat(),
            conflicts=report.conflicts,
            rejected=report.rejected,
        )
    return report


# --------------------------------------------------------------------------- push-ack


def push_ack(transport: Transport, layout: DataLayout, config: Config) -> int:
    """Call ``archive confirm --sha`` on the server for delivered files.

    Also sends the outbox (rows created on the computer) to the server inbox and runs
    ``bridge reconcile`` there. Returns the number of acknowledged files; raises
    :class:`PartialFailure` when some confirmation failed (the others are kept)."""
    acked = 0
    failures: list[str] = []
    for row in pending_acks(layout):
        sha = str(row["sha256"])
        code, _out, err = transport.run(
            kallimachos_argv(transport, "archive", "confirm", "--sha", sha)
        )
        if code == 0:
            store.append_jsonl(
                layout.bridge_delivered_file,
                {**row, "at": _now().isoformat(), "state": "acked"},
            )
            acked += 1
        else:
            failures.append(f"{row.get('path')}: exit {code}: {err.strip()[:200]}")
    outbox = outbox_file(layout)
    if outbox.exists() and outbox.stat().st_size > 0:
        digest = hashlib.sha256(outbox.read_bytes()).hexdigest()[:8]
        stamp = f"{_now():%Y%m%dT%H%M%SZ}-{digest}"
        transport.push(outbox, f"{REMOTE_INBOX}/{stamp}.jsonl")
        code, _out, err = transport.run(kallimachos_argv(transport, "bridge", "reconcile"))
        if code == 0:
            sent = layout.bridge_outbox_sent
            sent.mkdir(parents=True, exist_ok=True)
            os.replace(outbox, sent / f"{stamp}.jsonl")
        else:
            failures.append(f"server reconcile: exit {code}: {err.strip()[:200]}")
    if failures:
        raise PartialFailure("push-ack incomplete: " + "; ".join(failures))
    return acked


# --------------------------------------------------------------------------- confirm (server)


def confirm(layout: DataLayout, sha256: str, *, dry_run: bool = False) -> str:
    """Server side of push-ack (``archive confirm --sha``): ``outgoing -> stored`` after the server
    re-verifies its own copy; the file moves ``outgoing/`` -> ``delivered/`` without overwrite."""
    sha = sha256.strip().lower()
    entries = load_entries(layout.catalog_file)
    matches = [e for e in entries if e.sha256 == sha]
    if not matches:
        raise UsageError(f"no catalog entry with sha256 {sha[:12]}")
    entry = next((e for e in matches if e.status is CatalogStatus.OUTGOING), None)
    if entry is None:
        if any(ev.type is CatalogEventType.DELIVERED for e in matches for ev in e.events):
            return f"already confirmed: {matches[0].id}"
        raise UsageError(f"catalog entry {matches[0].id} is not outgoing ({matches[0].status})")
    if (
        not entry.filename
        or not safe_folder(entry.folder or "")
        or not SAFE_NAME_RE.match(entry.filename)
    ):
        raise UsageError(f"catalog entry {entry.id} has no valid folder/filename")
    rel = f"{entry.folder}/{entry.filename}" if entry.folder else entry.filename
    source = outgoing_dir(layout) / rel
    target = delivered_dir(layout) / rel
    moved = False
    if is_regular_file(source):
        if sha256_file(source) != sha:
            raise BridgeConflict(f"SHA_MISMATCH: server copy of {entry.id} differs; not confirmed")
        if target.exists():
            if not is_regular_file(target) or sha256_file(target) != sha:
                raise BridgeConflict(f"CONFLICT: delivered/{rel} exists with different content")
        elif not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            move_exclusive(source, target, sha)
            moved = True
    elif not (is_regular_file(target) and sha256_file(target) == sha):
        raise UsageError(f"outgoing file of {entry.id} is missing on the server")
    if dry_run:
        return f"would confirm: {entry.id}"
    entry.status = CatalogStatus.STORED
    entry.events.append(
        CatalogEvent(
            at=_now(),
            type=CatalogEventType.DELIVERED,
            detail={"sha256": sha, "moved": moved},
        )
    )
    save_entries(layout.catalog_file, entries)
    return f"confirmed: {entry.id}"


# --------------------------------------------------------------------------- reconcile


def reconcile(layout: DataLayout, config: Config) -> int:
    """Merge local and remote catalog copies by id with ``events: merged``.

    Computer: merges ``bridge/incoming/catalog.jsonl`` (the server copy) into the local catalog and
    queues local-only rows in the outbox for the server. Server: merges every
    ``bridge/inbox/*.jsonl`` and closes the unreachable rows they resolve; merged inbox files are
    renamed ``*.merged`` (kept). Returns the number of added + changed rows. A sha256 clash raises
    :class:`BridgeConflict` before anything is written."""
    now = _now()
    mine = load_entries(layout.catalog_file)
    count = 0
    only_local: list[str] = []
    server_copy = cache_catalog(layout)
    if server_copy.exists():
        res = merge(mine, load_entries(server_copy), source="server", now=now)
        mine, count, only_local = res.entries, len(res.added) + len(res.changed), res.only_mine
    inbox_files = sorted(inbox_dir(layout).glob("*.jsonl")) if inbox_dir(layout).is_dir() else []
    supplied: list[CatalogEntry] = []
    for path in inbox_files:
        rows = load_entries(path)
        res = merge(mine, rows, source="computer", now=now, prefer_theirs_on_tie=False)
        mine = res.entries
        count += len(res.added) + len(res.changed)
        supplied += rows
    if count:
        save_entries(layout.catalog_file, mine)
    if only_local:
        by_id = {e.id: e for e in mine}
        _queue_outbox(layout, [by_id[i] for i in only_local])
    if supplied:
        _close_unreachable(layout, supplied)
    for path in inbox_files:
        os.replace(path, path.with_name(path.name + ".merged"))
    if server_copy.exists() or inbox_files:
        _update_state(layout, reconciled_at=now.isoformat(), catalog="reconciled")
    return count


def _queue_outbox(layout: DataLayout, rows: list[CatalogEntry]) -> None:
    path = outbox_file(layout)
    queued = {e.id: e for e in load_entries(path)}
    for row in rows:
        queued[row.id] = row
    save_entries(path, list(queued.values()))


def _close_unreachable(layout: DataLayout, rows: list[CatalogEntry]) -> None:
    closing: dict[str, str] = {}
    for row in rows:
        for ev in row.events:
            uid = (
                ev.detail.get("unreachable_id")
                if ev.type is CatalogEventType.USER_SUPPLIED
                else None
            )
            if isinstance(uid, str):
                closing[uid] = f"user_supplied:{row.id}"
    path = layout.unreachable_file
    if not closing or not path.exists():
        return
    lines = list(store.read_jsonl(path))
    changed = False
    for line in lines:
        if line.get("id") in closing and line.get("status") == "open":
            line["status"] = "closed"
            line["closed_by"] = closing[str(line["id"])]
            changed = True
    if changed:
        for i, line in enumerate(lines):
            build(UnreachableEntry, line, path=f"unreachable[{i}]")
        store.write_jsonl_atomic(path, lines)


# --------------------------------------------------------------------------- _Incoming


def default_namer(config: Config) -> Namer:
    """Target of an unreachable work: the ``folder``/``filename`` the archive recorded on the
    entry when it has both and they are safe, else ``_Unsorted/<archive file name>``
    (``archive.naming``)."""

    def namer(entry: UnreachableEntry) -> tuple[str, str]:
        from kallimachos.archive.naming import build_filename
        from kallimachos.interfaces import Author, Record

        if (
            entry.filename
            and entry.folder is not None
            and safe_folder(entry.folder)
            and SAFE_NAME_RE.match(entry.filename)
        ):
            return entry.folder, entry.filename
        doi = next((k[4:] for k in entry.keys if k.startswith("doi:")), None)
        arxiv = next((k[6:] for k in entry.keys if k.startswith("arxiv:")), None)
        record = Record(
            title=entry.title,
            authors=[Author(name=a) for a in entry.authors],
            published=_dt.date(entry.year, 1, 1) if entry.year else None,
            venue=entry.venue,
            doi=doi,
            arxiv_id=arxiv,
        )
        name = build_filename(
            record, config.archive, arxiv_version=None, etal_suffix=config.notes.etal_suffix
        )
        return UNSORTED_FOLDER, name

    return namer


def _known_entries(layout: DataLayout) -> list[CatalogEntry]:
    by_id = {e.id: e for e in load_entries(layout.catalog_file)}
    for e in load_entries(cache_catalog(layout)):
        by_id[e.id] = e
    return list(by_id.values())


def _open_unreachable(layout: DataLayout) -> list[UnreachableEntry]:
    path = cache_unreachable(layout)
    if not path.exists():
        path = layout.unreachable_file
    if not path.exists():
        return []
    rows = [
        build(UnreachableEntry, r, path=f"unreachable[{i}]")
        for i, r in enumerate(store.read_jsonl(path))
    ]
    return [r for r in rows if r.status == "open"]


_STATUS_REPORT = {
    CatalogStatus.OUTGOING: ("PENDING_ON_SERVER", "the server will deliver this work"),
    CatalogStatus.STORED: ("ALREADY_IN_ARCHIVE", "the archive already has this work"),
    CatalogStatus.EXISTING: ("ALREADY_IN_ARCHIVE", "your existing library has this work"),
    CatalogStatus.REMOVED: ("REMOVED_FROM_ARCHIVE", "this work was removed from the archive"),
    CatalogStatus.IN_BOOK: ("IN_BOOK", "this work is part of a book in the archive"),
}


def process_incoming(
    layout: DataLayout,
    config: Config,
    *,
    dry_run: bool = False,
    read_text: TextReader | None = None,
    namer: Namer | None = None,
) -> DeliverReport:
    """User-dropped PDFs in ``_Incoming/``: identify, match exactly one catalog/unreachable entry,
    verify the title, place without overwriting; ambiguous ones go to ``_Incoming/_report.md``."""
    root = archive_root(config)
    drop = root / INCOMING_DIR_NAME
    report = DeliverReport()
    if not drop.is_dir() or drop.is_symlink():
        return report
    reader = read_text or pdftotext_first_pages
    name_for = namer or default_namer(config)
    catalog = _known_entries(layout)
    by_sha = {e.sha256: e for e in catalog if e.sha256}
    unreachable = _open_unreachable(layout)
    lines: list[tuple[str, str, str]] = []
    new_rows: list[CatalogEntry] = []
    for path in sorted(drop.iterdir()):
        name = path.name
        if name.startswith(".") or not name.lower().endswith(".pdf") or not is_regular_file(path):
            continue
        if is_dataless(path):
            report.deferred_dataless.append(path)
            lines.append((name, "WAITING", "cloud-only file; it is retried next round"))
            continue
        sha = sha256_file(path)
        if sha in by_sha:
            lines.append(
                (name, "ALREADY_IN_ARCHIVE", f"same content as catalog entry {by_sha[sha].id}")
            )
            continue
        text = reader(path)
        found = identities_from_filename(name) | identities_from_text(text)
        if not found:
            lines.append((name, "NO_IDENTITY", "no DOI/arXiv/ePrint id in the name or first pages"))
            continue
        cat_hits = [e for e in catalog if keys_intersect(e.keys, found)]
        unr_hits = [
            u
            for u in unreachable
            if keys_intersect(u.keys, found)
            and not any(keys_intersect(e.keys, set(u.keys)) for e in cat_hits)
        ]
        hits = len(cat_hits) + len(unr_hits)
        if hits == 0:
            lines.append((name, "NO_MATCH", "no catalog or unreachable entry has these ids"))
            continue
        if hits > 1:
            lines.append((name, "AMBIGUOUS", f"{hits} entries match; place it by hand"))
            continue
        if cat_hits:
            code, why = _STATUS_REPORT[cat_hits[0].status]
            lines.append((name, code, f"{why} ({cat_hits[0].id})"))
            continue
        entry = unr_hits[0]
        if not title_matches(entry.title, text):
            lines.append(
                (name, "TITLE_MISMATCH", f"title of {entry.id} not found on the first pages")
            )
            continue
        try:
            folder, filename = name_for(entry)
        except Exception as exc:  # naming failed for this work only
            lines.append((name, "NO_TARGET", f"no valid target name ({exc})"))
            continue
        if not safe_folder(folder) or not SAFE_NAME_RE.match(filename):
            lines.append((name, "NO_TARGET", f"invalid target {folder}/{filename}"))
            continue
        target = root / folder / filename if folder else root / filename
        if not inside_root(target, root):
            lines.append((name, "NO_TARGET", "target outside the archive root"))
            continue
        rel = f"{folder}/{filename}" if folder else filename
        if target.exists() or target.is_symlink():
            if is_regular_file(target) and not is_dataless(target) and sha256_file(target) == sha:
                lines.append((name, "ALREADY_IN_ARCHIVE", f"identical file at {rel}"))
            else:
                report.conflicts.append(f"CONFLICT (_Incoming target exists): {rel}")
                lines.append(
                    (name, "CONFLICT", f"{rel} exists with different content; left in place")
                )
            continue
        if dry_run:
            lines.append((name, "WOULD_PLACE", rel))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            move_exclusive(path, target, sha)
        except FileExistsError:
            report.conflicts.append(f"CONFLICT (_Incoming target appeared): {rel}")
            lines.append((name, "CONFLICT", f"{rel} appeared during the move; left in place"))
            continue
        row = _supplied_row(entry, folder, filename, sha, target.stat().st_size, name)
        new_rows.append(row)
        by_sha[sha] = row
        report.delivered.append(target)
        lines.append((name, "PLACED", rel))
    if not dry_run:
        if new_rows:
            save_entries(layout.catalog_file, [*load_entries(layout.catalog_file), *new_rows])
            _queue_outbox(layout, new_rows)
        if lines and not write_marked(drop / REPORT_NAME, _report_text(lines)):
            report.notes.append(f"{INCOMING_DIR_NAME}/{REPORT_NAME} is not ours; left untouched")
    report.notes += [f"{code}: {name}: {why}" for name, code, why in lines]
    return report


def _supplied_row(
    entry: UnreachableEntry, folder: str, filename: str, sha: str, size: int, source_name: str
) -> CatalogEntry:
    doi = next((k[4:] for k in entry.keys if k.startswith("doi:")), None)
    arxiv = next((k[6:] for k in entry.keys if k.startswith("arxiv:")), None)
    eprint = next((k[7:] for k in entry.keys if k.startswith("eprint:")), None)
    return CatalogEntry(
        id=sha[:12],
        keys=list(entry.keys),
        doi=doi,
        arxiv=arxiv,
        eprint=eprint,
        title=entry.title,
        authors=list(entry.authors[:30]),
        year=entry.year,
        venue=entry.venue,
        folder=folder,
        path=f"{folder}/{filename}" if folder else filename,
        filename=filename,
        sha256=sha,
        size=size,
        status=CatalogStatus.STORED,
        triggers=["user_supplied"],
        events=[
            CatalogEvent(
                at=_now(),
                type=CatalogEventType.USER_SUPPLIED,
                detail={"unreachable_id": entry.id, "source_name": source_name},
            )
        ],
    )


def _report_text(lines: list[tuple[str, str, str]]) -> str:
    out = [
        "# _Incoming report",
        "",
        f"Last run: {_now():%Y-%m-%d %H:%M} UTC",
        "",
        "A PDF dropped here is placed when its DOI/arXiv/ePrint id matches exactly one open",
        "unreachable entry and its title is found on the first pages. Anything else stays here.",
        "",
    ]
    out += [f"- **{code}** `{name}`: {why}" for name, code, why in lines]
    return "\n".join(out) + "\n"
