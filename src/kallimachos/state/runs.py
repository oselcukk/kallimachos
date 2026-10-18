"""Run identifiers and run-file bookkeeping.

``run_id`` helpers plus loading and saving of run and selection files.
"""

from __future__ import annotations

import datetime as _dt
import re

from kallimachos.interfaces import (
    RUN_ID_PATTERN,
    RunKind,
    RunResult,
    SchemaViolation,
    Selection,
    UsageError,
    build,
    dump,
)
from kallimachos.paths import DataLayout
from kallimachos.state import store

_RUN_ID_RE = re.compile(RUN_ID_PATTERN)


def run_id(kind: RunKind, day: _dt.date) -> str:
    """``YYYY-MM-DD-daily`` / ``YYYY-MM-DD-weekly``."""
    return f"{day.isoformat()}-{kind.value}"


def parse_run_id(value: str) -> tuple[RunKind, _dt.date]:
    """Inverse of :func:`run_id`; raises :class:`UsageError` (exit 2) on a malformed id."""
    if not _RUN_ID_RE.match(value):
        raise UsageError(f"malformed run id {value!r} (expected YYYY-MM-DD-daily|weekly)")
    day, kind = value[:10], value[11:]
    return RunKind(kind), _dt.date.fromisoformat(day)


def load_run(layout: DataLayout, rid: str) -> RunResult:
    """Read ``runs/<run_id>.json``; raise ``UsageError`` when missing."""
    parse_run_id(rid)
    data = store.read_json(layout.run_file(rid))
    if data is None:
        raise UsageError(f"no run file for {rid} in {layout.runs}")
    return build(RunResult, data, path=f"runs/{rid}.json")


def save_run(layout: DataLayout, run: RunResult) -> None:
    """Write ``runs/<run_id>.json`` atomically after schema validation."""
    parse_run_id(run.run_id)
    store.write_json_atomic(
        layout.run_file(run.run_id), dump(run), schema=store.schema_for("run.schema.json")
    )


def load_selection(layout: DataLayout, rid: str) -> Selection | None:
    """Read ``runs/<run_id>.selection.json`` or ``None``."""
    parse_run_id(rid)
    data = store.read_json(layout.selection_file(rid))
    if data is None:
        return None
    return build(Selection, data, path=f"runs/{rid}.selection.json")


def save_selection(layout: DataLayout, selection: Selection) -> None:
    """Write ``runs/<run_id>.selection.json`` atomically after validation."""
    parse_run_id(selection.run_id)
    store.write_json_atomic(
        layout.selection_file(selection.run_id),
        dump(selection),
        schema=store.schema_for("selection.schema.json"),
    )


def run_failed(run: RunResult) -> bool:
    """A run is fatal when it recorded errors and no source succeeded (exit 6 territory).

    A partial run (some sources failed, others delivered) still counts as successful.
    """
    if not run.errors:
        return False
    if not run.sources:
        return True
    return all(stats.errors for stats in run.sources.values())


def list_run_ids(layout: DataLayout, kind: RunKind | None = None) -> list[str]:
    """Run ids that have a run file, oldest first."""
    if not layout.runs.is_dir():
        return []
    out: list[str] = []
    for path in layout.runs.glob("*.json"):
        rid = path.name[: -len(".json")]
        if not _RUN_ID_RE.match(rid):
            continue  # selection files and anything else
        if kind is not None and not rid.endswith(f"-{kind.value}"):
            continue
        out.append(rid)
    return sorted(out)


def last_successful_run_date(layout: DataLayout, kind: RunKind) -> _dt.date | None:
    """Newest run date with a run file and no fatal error;
    used by ``schedule run-missed``.

    Unreadable run files are skipped (``doctor`` reports them).
    """
    for rid in reversed(list_run_ids(layout, kind)):
        try:
            run = load_run(layout, rid)
        except (SchemaViolation, UsageError):
            continue
        if not run_failed(run):
            return parse_run_id(rid)[1]
    return None
