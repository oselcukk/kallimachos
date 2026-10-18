"""``process/status.jsonl`` (append-only) and the ``process status`` table.

Every transition is written by a CLI command through :func:`record_state`; agents never edit the
file by hand. The last line per eid wins.
"""

from __future__ import annotations

import datetime as _dt
from collections import Counter

from kallimachos.i18n import t
from kallimachos.interfaces import SCHEMA_REGISTRY, StatusEntry, WorkState, build, dump, json_schema
from kallimachos.paths import DataLayout
from kallimachos.state import store
from kallimachos.state.machine import PROCESS_ORDER, assert_transition

STATUS_SCHEMA = json_schema(SCHEMA_REGISTRY["status.schema.json"][0])


def read_entries(layout: DataLayout) -> list[StatusEntry]:
    """Every line of ``status.jsonl`` in file order (missing file -> empty list)."""
    return [
        build(StatusEntry, row, path=f"status.jsonl[{i}]")
        for i, row in enumerate(store.read_jsonl(layout.status_file))
    ]


def current_states(layout: DataLayout) -> dict[str, StatusEntry]:
    """eid -> last entry."""
    out: dict[str, StatusEntry] = {}
    for entry in read_entries(layout):
        out[entry.eid] = entry
    return out


def current_state(layout: DataLayout, eid: str) -> WorkState | None:
    entry = current_states(layout).get(eid)
    return entry.state if entry else None


def record_state(
    layout: DataLayout, eid: str, state: WorkState, *, by: str, at: _dt.datetime | None = None
) -> StatusEntry:
    """Validate the transition (``state.machine``) and append.

    Recording the state an eid already has is a no-op (the existing entry is returned), so a
    corrected card can be re-delivered without a self-loop in the transition table.
    """
    current = current_states(layout).get(eid)
    if current is not None and current.state == state:
        return current
    assert_transition(current.state if current else None, state)
    entry = StatusEntry(eid=eid, state=state, at=at or _dt.datetime.now(tz=_dt.UTC), by=by)
    store.append_jsonl(layout.status_file, dump(entry), schema=STATUS_SCHEMA)
    return entry


def check_transition(layout: DataLayout, eid: str, state: WorkState) -> None:
    """Raise :class:`UsageError` when ``state`` cannot be recorded for ``eid`` right now.

    Commands call this before writing a file so a refused transition leaves nothing behind.
    """
    current = current_state(layout, eid)
    if current != state:
        assert_transition(current, state)


def ensure_started(
    layout: DataLayout, eid: str, *, by: str, at: _dt.datetime | None = None
) -> StatusEntry:
    """Give an eid that has no status yet the ``downloaded`` entry state.

    ``process extract`` is the first processing command; a PDF that the archive placed before the
    state machine existed (or that was handed in by path) starts here.
    """
    current = current_states(layout).get(eid)
    if current is not None:
        return current
    return record_state(layout, eid, WorkState.DOWNLOADED, by=by, at=at)


def status_table(layout: DataLayout) -> list[dict[str, str]]:
    rows = [
        {"eid": e.eid, "state": e.state.value, "at": e.at.isoformat(), "by": e.by}
        for e in current_states(layout).values()
    ]
    rows.sort(key=lambda r: (_order(r["state"]), r["eid"]))
    return rows


def _order(state: str) -> int:
    try:
        return PROCESS_ORDER.index(WorkState(state))
    except ValueError:
        return -1


LEARNED_LABEL = "Öğrenilen:"
"""Receipt label the Avenox beyin skill requires verbatim (language independent)."""


def summary_text(layout: DataLayout, *, lang: str) -> str:
    """Receipt summary text for ``process status --summary``; prints only.

    Labels come from the ``process.summary.*`` i18n keys; state names stay as in ``status.jsonl``.
    """
    states = current_states(layout)
    counts = Counter(entry.state for entry in states.values())
    lines = [t("process.summary.tracked", lang, count=len(states))]
    for state in PROCESS_ORDER:
        if counts.get(state):
            lines.append(t("process.summary.state", lang, state=state.value, count=counts[state]))
    for state in (WorkState.CHECK_FAILED, WorkState.SEMANTIC_FAILED):
        if counts.get(state):
            lines.append(
                t("process.summary.needs_correction", lang, state=state.value, count=counts[state])
            )
    pending = [
        eid
        for eid, entry in sorted(states.items())
        if entry.state in PROCESS_ORDER and entry.state != WorkState.CLUSTER_SUMMARIZED
    ]
    if pending:
        lines.append(t("process.summary.open_steps", lang, count=len(pending)))
    # The beyin skill (Avenox) looks for this exact label in every receipt, in any UI language:
    # "Öğrenilen: <one sentence>" or "Öğrenilen: yok". Only the hint after it is translated.
    lines.append(LEARNED_LABEL + " " + t("process.summary.learned", lang))
    return "\n".join(lines)
