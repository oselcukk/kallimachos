"""Candidate -> work state machine.

Every transition is written by a CLI command; agents never edit ``status.jsonl`` by hand.
This module is data plus a check; the command that writes a transition calls
:func:`assert_transition`
before appending a :class:`~kallimachos.interfaces.StatusEntry`.
"""

from __future__ import annotations

from kallimachos.interfaces import UsageError, WorkState

S = WorkState

ALLOWED_TRANSITIONS: dict[WorkState, frozenset[WorkState]] = {
    # run
    S.DISCOVERED: frozenset({S.SCORED}),
    S.SCORED: frozenset({S.DROPPED, S.CANDIDATE}),
    S.DROPPED: frozenset(),
    # run + select
    S.CANDIDATE: frozenset({S.SHOWN, S.UNSHOWN}),
    S.UNSHOWN: frozenset({S.EXPIRED, S.SHOWN}),
    S.EXPIRED: frozenset(),
    # feedback
    S.SHOWN: frozenset({S.FB_GOOD, S.FB_IRRELEVANT, S.FB_READ, S.FB_ASK, S.FB_ARCHIVE}),
    S.FB_GOOD: frozenset({S.ARCHIVE_QUEUED}),
    S.FB_READ: frozenset({S.ARCHIVE_QUEUED}),
    S.FB_ASK: frozenset({S.ARCHIVE_QUEUED}),
    S.FB_ARCHIVE: frozenset({S.ARCHIVE_QUEUED}),
    S.FB_IRRELEVANT: frozenset({S.FB_GOOD}),  # a later "good" overrides; last decision wins
    S.AUTO_PASS: frozenset({S.ARCHIVE_QUEUED}),
    # archive
    S.ARCHIVE_QUEUED: frozenset({S.DOWNLOADED, S.UNREACHABLE, S.EXISTING, S.FILTERED}),
    S.UNREACHABLE: frozenset({S.DOWNLOADED}),  # archive retry
    S.EXISTING: frozenset({S.TEXT_EXTRACTED}),
    S.FILTERED: frozenset(),
    # process
    S.DOWNLOADED: frozenset({S.TEXT_EXTRACTED}),
    S.TEXT_EXTRACTED: frozenset({S.OCR_DONE, S.CARD_WRITTEN}),
    S.OCR_DONE: frozenset({S.CARD_WRITTEN}),
    S.CARD_WRITTEN: frozenset({S.CHECK_PASSED, S.CHECK_FAILED}),
    S.CHECK_FAILED: frozenset({S.CARD_WRITTEN}),  # corrected card re-delivered
    S.CHECK_PASSED: frozenset({S.SEMANTIC_VERIFIED, S.SEMANTIC_FAILED}),
    S.SEMANTIC_FAILED: frozenset({S.SEMANTIC_VERIFIED, S.CARD_WRITTEN}),
    S.SEMANTIC_VERIFIED: frozenset({S.NOTE_WRITTEN}),
    S.NOTE_WRITTEN: frozenset({S.LINKED, S.NOTE_WRITTEN}),  # --existing rewrite is idempotent
    S.LINKED: frozenset({S.CLUSTER_SUMMARIZED, S.LINKED}),
    S.CLUSTER_SUMMARIZED: frozenset({S.CLUSTER_SUMMARIZED}),
}

PROCESS_ORDER: tuple[WorkState, ...] = (
    S.DOWNLOADED,
    S.TEXT_EXTRACTED,
    S.OCR_DONE,
    S.CARD_WRITTEN,
    S.CHECK_PASSED,
    S.SEMANTIC_VERIFIED,
    S.NOTE_WRITTEN,
    S.LINKED,
    S.CLUSTER_SUMMARIZED,
)
"""Linear processing progression used by gates such as "all eids >= linked"."""


def can_transition(current: WorkState, new: WorkState) -> bool:
    return new in ALLOWED_TRANSITIONS.get(current, frozenset())


def assert_transition(current: WorkState | None, new: WorkState) -> None:
    """Raise :class:`UsageError` (exit 2) when ``current -> new`` is not allowed.

    ``current=None`` means the eid has no status yet; only entry states are allowed then.
    """
    if current is None:
        if new in (S.DISCOVERED, S.AUTO_PASS, S.ARCHIVE_QUEUED, S.DOWNLOADED, S.EXISTING):
            return
        raise UsageError(f"cannot start an eid in state {new.value!r}")
    if not can_transition(current, new):
        raise UsageError(f"illegal state transition {current.value!r} -> {new.value!r}")


def at_least(state: WorkState, floor: WorkState) -> bool:
    """True when ``state`` is at or beyond ``floor`` in :data:`PROCESS_ORDER`.

    ``check_failed`` / ``semantic_failed`` count as their preceding passed state not reached.
    """
    if state not in PROCESS_ORDER or floor not in PROCESS_ORDER:
        return False
    return PROCESS_ORDER.index(state) >= PROCESS_ORDER.index(floor)
