"""``state/seen.jsonl`` index: seen is not shown.

A candidate that entered a run but was not shown in
the message is *not* lost. It competes again in every run for ``rescan.unshown_grace_days``
(counted from the run it first appeared in) and only then expires. A preprint that was shown and
later reappears as its journal version is allowed once more; the entry records that in
``journal_version_note`` so it happens exactly once.
"""

from __future__ import annotations

import datetime as _dt
import enum
from collections.abc import Sequence
from pathlib import Path

from kallimachos.interfaces import Record, SeenEntry, WorkKind, build
from kallimachos.state import store
from kallimachos.state.runs import parse_run_id

TITLE_SHORT_CHARS = 60

__all__ = ["SeenDecision", "SeenIndex", "seen_decision"]


class SeenDecision(enum.StrEnum):
    NEW = "new"  # never seen
    SHOWN = "shown"  # shown before: blocked
    EXPIRED = "expired"  # unshown and past grace: blocked
    GRACE = "grace"  # unshown, still within grace: competes again
    JOURNAL_VERSION = "journal_version"  # preprint shown before, now a journal version: once more


def _run_date(run_id: str) -> _dt.date:
    return parse_run_id(run_id)[1]


def _is_journal_kind(kind: WorkKind) -> bool:
    return kind in (WorkKind.ARTICLE, WorkKind.CHAPTER)


def seen_decision(
    entry: SeenEntry | None, kind: WorkKind, *, today: _dt.date, grace_days: int
) -> SeenDecision:
    """Pure decision shared by :class:`SeenIndex` and ``rules.core.SeenRule``."""
    if entry is None:
        return SeenDecision.NEW
    if entry.shown:
        if (
            entry.kind is WorkKind.PREPRINT
            and _is_journal_kind(kind)
            and entry.journal_version_note is None
        ):
            return SeenDecision.JOURNAL_VERSION
        return SeenDecision.SHOWN
    if entry.expired:
        return SeenDecision.EXPIRED
    age = (today - _run_date(entry.first_run)).days
    if age > grace_days:
        return SeenDecision.EXPIRED
    return SeenDecision.GRACE


def _title_short(title: str) -> str:
    title = " ".join(title.split())
    return title if len(title) <= TITLE_SHORT_CHARS else title[: TITLE_SHORT_CHARS - 1] + "~"


def _seen_kind(record_kind: WorkKind) -> WorkKind:
    """seen.jsonl distinguishes preprint vs journal (``article``) only."""
    if record_kind is WorkKind.PREPRINT:
        return WorkKind.PREPRINT
    if _is_journal_kind(record_kind):
        return WorkKind.ARTICLE
    return record_kind


class SeenIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: list[SeenEntry] = []
        self._by_key: dict[str, SeenEntry] = {}

    # ------------------------------------------------------------------ io
    @classmethod
    def load(cls, path: Path) -> SeenIndex:
        index = cls(path)
        for raw in store.read_jsonl(path):
            index._add(build(SeenEntry, raw, path="seen"))
        return index

    def save(self) -> None:
        """Rewrite the file atomically through ``state.store.write_jsonl_atomic``."""
        store.write_jsonl_atomic(
            self.path, self._entries, schema=store.schema_for("seen.schema.json")
        )

    # ------------------------------------------------------------------ access
    def _add(self, entry: SeenEntry) -> None:
        self._entries.append(entry)
        for key in entry.keys:
            self._by_key.setdefault(key, entry)

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._entries)

    @property
    def entries(self) -> list[SeenEntry]:
        return list(self._entries)

    def mapping(self) -> dict[str, SeenEntry]:
        """``key -> entry`` view for :class:`~kallimachos.interfaces.RuleContext.seen`."""
        return dict(self._by_key)

    def lookup(self, keys: Sequence[str]) -> SeenEntry | None:
        for key in keys:
            entry = self._by_key.get(key)
            if entry is not None:
                return entry
        return None

    def decision(
        self, keys: Sequence[str], kind: WorkKind, *, today: _dt.date, grace_days: int
    ) -> SeenDecision:
        return seen_decision(self.lookup(keys), kind, today=today, grace_days=grace_days)

    def is_blocked(
        self,
        record: Record,
        *,
        today: _dt.date,
        grace_days: int,
        keys: Sequence[str] | None = None,
    ) -> bool:
        """True when shown before, or unshown and past the grace window (expired). A preprint that
        reappears as a journal version is allowed once more (``journal_version_note``).

        ``keys`` defaults to ``records.keys.keys_for(record)``.
        """
        if keys is None:
            from kallimachos.records.keys import keys_for

            keys = keys_for(record)
        decision = self.decision(keys, record.kind, today=today, grace_days=grace_days)
        return decision in (SeenDecision.SHOWN, SeenDecision.EXPIRED)

    # ------------------------------------------------------------------ update
    def record_run(
        self, record: Record, keys: Sequence[str], run_id: str, *, shown: bool, shown_no: int | None
    ) -> None:
        self._record(
            keys,
            run_id,
            record_kind=record.kind,
            title=record.title,
            shown=shown,
            shown_no=shown_no,
        )

    def mark_shown(
        self, keys: Sequence[str], run_id: str, *, kind: WorkKind, title: str, shown_no: int | None
    ) -> None:
        """Mark a candidate of ``run_id`` as shown when the message goes out outside Kallimachos
        (``kallimachos select``: the agent or Hermes delivers it). Same entry shape as
        :meth:`record_run`; calling it again for the same run changes nothing."""
        self._record(keys, run_id, record_kind=kind, title=title, shown=True, shown_no=shown_no)

    def _record(
        self,
        keys: Sequence[str],
        run_id: str,
        *,
        record_kind: WorkKind,
        title: str,
        shown: bool,
        shown_no: int | None,
    ) -> None:
        kind = _seen_kind(record_kind)
        entry = self.lookup(keys)
        if entry is None:
            entry = SeenEntry(
                keys=list(dict.fromkeys(keys)),
                first_run=run_id,
                last_run=run_id,
                shown=shown,
                shown_run=run_id if shown else None,
                shown_no=shown_no if shown else None,
                kind=kind,
                title_short=_title_short(title),
            )
            self._add(entry)
            return
        for key in keys:
            if key not in entry.keys:
                entry.keys.append(key)
                self._by_key.setdefault(key, entry)
        entry.last_run = run_id
        if entry.shown and entry.kind is WorkKind.PREPRINT and kind is WorkKind.ARTICLE:
            entry.journal_version_note = f"journal version seen in {run_id}"
            entry.kind = WorkKind.ARTICLE
        elif kind is not WorkKind.UNKNOWN:
            entry.kind = kind
        if shown:
            entry.shown = True
            entry.shown_run = run_id
            entry.shown_no = shown_no
            entry.expired = False
        if not entry.title_short:
            entry.title_short = _title_short(title)

    def undo_run(self, run_id: str) -> int:
        """Forget what ``run_id`` recorded so that ``--rerun`` recomputes it from scratch: entries
        the run introduced are dropped, entries it marked shown become unshown again. Returns the
        number of entries touched."""
        touched = 0
        kept: list[SeenEntry] = []
        for entry in self._entries:
            if entry.first_run == run_id:
                touched += 1
                continue
            if entry.shown and entry.shown_run == run_id:
                entry.shown = False
                entry.shown_run = None
                entry.shown_no = None
                touched += 1
            kept.append(entry)
        if touched:
            self._entries = []
            self._by_key = {}
            for entry in kept:
                self._add(entry)
        return touched

    def expire(self, *, today: _dt.date, grace_days: int) -> int:
        count = 0
        for entry in self._entries:
            if entry.shown or entry.expired:
                continue
            if (today - _run_date(entry.first_run)).days > grace_days:
                entry.expired = True
                count += 1
        return count
