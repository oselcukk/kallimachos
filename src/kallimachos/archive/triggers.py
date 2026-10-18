"""Archive triggers.

Sources of triggers: feedback tags ``good|read|ask|archive``, certain-pass candidates
(``auto_pass``), always-show authors' new works in weekly runs (``always_show_new``) and
``archive/manual.txt``. Triggers with the same identity merge into one; the merged trigger keeps
the strongest kind (an explicit user request is exempt from the filter).

``manual.txt`` grammar, one work per line::

    # comment line                      (also: text after " #" is a comment)
    10.0000/example.1                   DOI, doi:..., https://doi.org/...
    2501.01234v2  channel=A             arXiv id (new/old style, arXiv:..., abs/pdf URL)
    2024/123      person=Example-Author IACR ePrint id
    W1234567                            OpenAlex work id

Options are ``key=value`` tokens after the identifier: ``channel=<channel id>`` and
``person=<folder>``. Unknown identifiers or options are reported, never guessed.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace

from kallimachos.archive.identity import key_of
from kallimachos.interfaces import ArchiveTrigger, FeedbackEntry, FeedbackTag, RunKind, RunResult
from kallimachos.paths import DataLayout
from kallimachos.profile.schema import Profile

ARCHIVE_TAGS: frozenset[FeedbackTag] = frozenset(
    {FeedbackTag.GOOD, FeedbackTag.READ, FeedbackTag.ASK, FeedbackTag.ARCHIVE}
)
_FOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_CHANNEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,15}$")

# merge priority: lower wins
_PRIORITY = {"manual": 0, "feedback": 1, "always_show_new": 2, "auto_pass": 3}


def parse_manual(text: str, *, errors: list[str] | None = None) -> list[ArchiveTrigger]:
    """``manual.txt``: one DOI/arXiv/ePrint/W id per line, ``# comment``, ``channel=X``,
    ``person=<folder>``. Problems are appended to ``errors`` (line-numbered)."""
    out: list[ArchiveTrigger] = []
    problems = errors if errors is not None else []
    for no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if " #" in line:
            line = line[: line.index(" #")].strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        key = key_of(tokens[0])
        if key is None or not key.startswith(("doi:", "arxiv:", "eprint:", "oa:")):
            problems.append(f"line {no}: unrecognised identifier {tokens[0]!r}")
            continue
        channel: str | None = None
        person: str | None = None
        bad = False
        for tok in tokens[1:]:
            name, sep, value = tok.partition("=")
            if sep and name == "channel" and _CHANNEL_RE.match(value):
                channel = value
            elif sep and name == "person" and _FOLDER_RE.match(value):
                person = value
            else:
                problems.append(f"line {no}: unknown option {tok!r}")
                bad = True
        if bad:
            continue
        out.append(
            ArchiveTrigger(
                kind="manual",
                ident=key,
                channel_hint=channel,
                person_folder=person,
                note=f"manual:{no}",
            )
        )
    return out


def is_exempt(trigger: ArchiveTrigger) -> bool:
    """Explicit user requests skip the filter."""
    return trigger.kind == "manual" or (
        trigger.kind == "feedback" and trigger.note == FeedbackTag.ARCHIVE.value
    )


def label(trigger: ArchiveTrigger) -> str:
    """Catalog ``triggers[]`` label, e.g. ``feedback:archive:2026-10-18-daily``."""
    parts: list[str] = [trigger.kind]
    if trigger.kind == "feedback" and trigger.note:
        parts.append(trigger.note)
    if trigger.kind == "manual" and trigger.note:
        return trigger.note
    if trigger.run_id:
        parts.append(trigger.run_id)
    return ":".join(parts)


def from_feedback(entries: Sequence[FeedbackEntry]) -> list[ArchiveTrigger]:
    out: list[ArchiveTrigger] = []
    for e in entries:
        if e.tag not in ARCHIVE_TAGS:
            continue
        key = key_of(e.candidate_id)
        if key is None:
            continue
        out.append(ArchiveTrigger(kind="feedback", ident=key, run_id=e.run_id, note=e.tag.value))
    return out


def from_runs(runs: Sequence[RunResult]) -> list[ArchiveTrigger]:
    out: list[ArchiveTrigger] = []
    for run in runs:
        for cand in run.candidates:
            key = key_of(cand.id)
            if key is None:
                continue
            hint = cand.channels[0] if cand.channels else None
            if cand.certain_pass:
                out.append(
                    ArchiveTrigger(
                        kind="auto_pass", ident=key, run_id=run.run_id, channel_hint=hint
                    )
                )
            if cand.always_show_author and run.kind is RunKind.WEEKLY:
                out.append(ArchiveTrigger(kind="always_show_new", ident=key, run_id=run.run_id))
    return out


def merge(triggers: Sequence[ArchiveTrigger]) -> list[ArchiveTrigger]:
    """Same identity -> one trigger: strongest kind wins; channel/person hints are filled from
    the others when the winner has none. Order of first appearance is kept."""
    by_key: dict[str, ArchiveTrigger] = {}
    for t in triggers:
        old = by_key.get(t.ident)
        if old is None:
            by_key[t.ident] = t
            continue
        exempt_new = is_exempt(t) and not is_exempt(old)
        stronger = exempt_new or (
            is_exempt(t) == is_exempt(old) and _PRIORITY[t.kind] < _PRIORITY[old.kind]
        )
        win, lose = (t, old) if stronger else (old, t)
        by_key[t.ident] = replace(
            win,
            channel_hint=win.channel_hint or lose.channel_hint,
            person_folder=win.person_folder or lose.person_folder,
        )
    return list(by_key.values())


def collect_triggers(
    layout: DataLayout,
    feedback: Sequence[FeedbackEntry],
    runs: Sequence[RunResult],
    profile: Profile,
    *,
    errors: list[str] | None = None,
) -> list[ArchiveTrigger]:
    """feedback (good/read/ask/archive) + auto_pass + always_show_new + manual.

    Merged by identity.
    """
    manual: list[ArchiveTrigger] = []
    if layout.manual_file.is_file():
        manual = parse_manual(layout.manual_file.read_text(encoding="utf-8"), errors=errors)
    known = {ch.id for ch in profile.channels}
    for t in manual:
        if t.channel_hint and t.channel_hint not in known and errors is not None:
            errors.append(f"manual: unknown channel {t.channel_hint!r} for {t.ident}")
    return merge([*manual, *from_feedback(feedback), *from_runs(runs)])
