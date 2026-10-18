"""state.runs: run identifiers."""

from __future__ import annotations

import datetime as dt

import pytest

from kallimachos.interfaces import RunKind, UsageError
from kallimachos.state.runs import parse_run_id, run_id


def test_round_trip() -> None:
    rid = run_id(RunKind.WEEKLY, dt.date(2026, 10, 18))
    assert rid == "2026-10-18-weekly"
    assert parse_run_id(rid) == (RunKind.WEEKLY, dt.date(2026, 10, 18))


@pytest.mark.parametrize("bad", ["2026-10-18", "2026-10-18-monthly", "20261018-daily", ""])
def test_malformed(bad: str) -> None:
    with pytest.raises(UsageError):
        parse_run_id(bad)
