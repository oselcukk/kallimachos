"""``state/feedback.jsonl`` access and item-number resolution.

Every write goes through ``state.store.append_jsonl`` with the generated feedback schema, so a
line is validated and NFC-normalised before it lands on disk.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache
from typing import Any

from kallimachos.interfaces import (
    FeedbackEntry,
    FeedbackTag,
    RunResult,
    Selection,
    UsageError,
    build,
    dump,
    json_schema,
)
from kallimachos.paths import DataLayout
from kallimachos.state import store

__all__ = ["append_feedback", "effective_tags", "load_feedback", "resolve_item"]


@cache
def _schema() -> dict[str, Any]:
    return json_schema(FeedbackEntry)


def load_feedback(layout: DataLayout) -> list[FeedbackEntry]:
    return [
        build(FeedbackEntry, raw, path="feedback") for raw in store.read_jsonl(layout.feedback_file)
    ]


def append_feedback(layout: DataLayout, entry: FeedbackEntry) -> bool:
    """Append unless (run_id, candidate_id, tag) exists; return whether written."""
    key = (entry.run_id, entry.candidate_id, entry.tag)
    for existing in load_feedback(layout):
        if (existing.run_id, existing.candidate_id, existing.tag) == key:
            return False
    store.append_jsonl(layout.feedback_file, dump(entry), schema=_schema())
    return True


def effective_tags(entries: Sequence[FeedbackEntry]) -> dict[str, FeedbackTag]:
    """candidate_id -> last decision (``irrelevant`` then ``good`` keeps both lines, last wins)."""
    out: dict[str, FeedbackTag] = {}
    for entry in sorted(entries, key=lambda e: e.at):
        out[entry.candidate_id] = entry.tag
    return out


def _normalise_item_no(item_no: str) -> str:
    text = item_no.strip()
    if text[:1].lower() == "h" and text[1:].isdigit():
        return "H" + str(int(text[1:]))
    if text.isdigit():
        return str(int(text))
    raise UsageError(f"item number {item_no!r} is neither <n> nor H<n>")


def resolve_item(run: RunResult, selection: Selection | None, item_no: str) -> tuple[str, str]:
    """Message number -> (candidate_id, resolved_via). Uses ``selection.message_no`` when present,
    else ``Candidate.no``. Unknown number is a ``UsageError``."""
    no = _normalise_item_no(item_no)
    if selection is not None and selection.message_no:
        cid = selection.message_no.get(no)
        if cid is None:
            raise UsageError(f"item {no} is not in the message of run {run.run_id}")
        return cid, "selection"
    if no.startswith("H"):
        raise UsageError(f"item {no} needs the selection file of run {run.run_id}")
    wanted = int(no)
    for cand in run.candidates:
        if cand.no == wanted:
            return cand.id, "run_no"
    raise UsageError(f"item {no} is not a candidate number of run {run.run_id}")
