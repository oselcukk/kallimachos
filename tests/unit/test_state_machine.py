"""state.machine: work-state transitions and gates."""

from __future__ import annotations

import pytest

from kallimachos.interfaces import UsageError
from kallimachos.interfaces import WorkState as S
from kallimachos.state.machine import (
    ALLOWED_TRANSITIONS,
    assert_transition,
    at_least,
    can_transition,
)


def test_every_state_has_a_row() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(S)


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (S.DISCOVERED, S.SCORED),
        (S.SCORED, S.DROPPED),
        (S.CANDIDATE, S.UNSHOWN),
        (S.UNSHOWN, S.EXPIRED),
        (S.SHOWN, S.FB_READ),
        (S.FB_IRRELEVANT, S.FB_GOOD),
        (S.AUTO_PASS, S.ARCHIVE_QUEUED),
        (S.ARCHIVE_QUEUED, S.UNREACHABLE),
        (S.DOWNLOADED, S.TEXT_EXTRACTED),
        (S.TEXT_EXTRACTED, S.CARD_WRITTEN),
        (S.CHECK_PASSED, S.SEMANTIC_FAILED),
        (S.SEMANTIC_VERIFIED, S.NOTE_WRITTEN),
        (S.NOTE_WRITTEN, S.LINKED),
        (S.LINKED, S.CLUSTER_SUMMARIZED),
    ],
)
def test_allowed(current: S, new: S) -> None:
    assert can_transition(current, new)
    assert_transition(current, new)


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (S.DROPPED, S.CANDIDATE),
        (S.CHECK_PASSED, S.NOTE_WRITTEN),  # semantic verification is mandatory
        (S.CHECK_FAILED, S.NOTE_WRITTEN),
        (S.SEMANTIC_FAILED, S.NOTE_WRITTEN),
        (S.DOWNLOADED, S.CARD_WRITTEN),
        (S.SHOWN, S.ARCHIVE_QUEUED),
    ],
)
def test_forbidden(current: S, new: S) -> None:
    assert not can_transition(current, new)
    with pytest.raises(UsageError, match="illegal state transition"):
        assert_transition(current, new)


def test_entry_states() -> None:
    assert_transition(None, S.DOWNLOADED)
    assert_transition(None, S.DISCOVERED)
    with pytest.raises(UsageError, match="cannot start"):
        assert_transition(None, S.NOTE_WRITTEN)


def test_at_least_for_gates() -> None:
    assert at_least(S.LINKED, S.LINKED)
    assert at_least(S.CLUSTER_SUMMARIZED, S.NOTE_WRITTEN)
    assert not at_least(S.NOTE_WRITTEN, S.LINKED)
    assert not at_least(S.CHECK_FAILED, S.CARD_WRITTEN)
