"""SessionStart status summary.

A few factual lines for the agent at session start: last scan, papers in the last message,
messages without a reply, reading queue. Small budget (``hooks.summary_max_chars``, default 600) so
it never crowds out other context (an Avenox vault injects its own memory first); silent when there
is nothing to report or anything fails.
"""

from __future__ import annotations

import datetime as _dt

from kallimachos.config.schema import Config
from kallimachos.feedback import store as fb_store
from kallimachos.paths import DataLayout
from kallimachos.state import runs as state_runs

UNANSWERED_LOOKBACK_DAYS = 7


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def build_summary(
    layout: DataLayout, config: Config, *, today: _dt.date | None = None
) -> str | None:
    """The summary text, or None when there is nothing worth saying."""
    if not layout.config_file.exists() and not layout.runs.is_dir():
        return None
    today = today or _dt.date.today()
    parts: list[str] = []
    ids = state_runs.list_run_ids(layout)
    if ids:
        rid = ids[-1]
        _kind, day = state_runs.parse_run_id(rid)
        run = state_runs.load_run(layout, rid)
        sel = state_runs.load_selection(layout, rid)
        shown = len(sel.main) if sel else 0
        age = (today - day).days
        when = "today" if age == 0 else ("yesterday" if age == 1 else f"{age} days ago")
        status = "failed" if state_runs.run_failed(run) else "ok"
        parts.append(
            f"Last scan {rid} ({when}, {status}): {shown} paper(s) in the message, "
            f"{len(run.candidates)} candidate(s)."
        )
    else:
        parts.append("No scan yet (first run: `kallimachos run daily --first-run`).")

    answered = {e.run_id for e in fb_store.load_feedback(layout)}
    unanswered: list[str] = []
    for rid in reversed(ids):
        _kind, day = state_runs.parse_run_id(rid)
        if (today - day).days > UNANSWERED_LOOKBACK_DAYS:
            break
        sel = state_runs.load_selection(layout, rid)
        if sel and sel.main and rid not in answered:
            unanswered.append(rid)
    if unanswered:
        parts.append(
            f"Messages without feedback: {len(unanswered)} ({', '.join(unanswered[:3])}); "
            "`kallimachos feedback poll` fetches replies."
        )

    if layout.reading_queue_file.exists():
        from kallimachos.process import queue as queue_mod

        current = queue_mod.current_entries(layout)
        reading = [e for (_i, kind), e in current.items() if kind == "read"]
        if reading:
            parts.append(f"Reading queue: {len(reading)} item(s).")

    if "email" in config.notify.channels and config.notify.email.confirmed_at is None:
        parts.append("E-mail channel not confirmed yet (`kallimachos notify confirm-email`).")

    text = "Kallimachos: " + " ".join(parts)
    return _shorten(text, config.hooks.summary_max_chars)
