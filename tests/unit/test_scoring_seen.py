"""seen.jsonl semantics: seen is not shown; grace; journal version once."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from kallimachos.interfaces import SeenEntry, WorkKind
from kallimachos.scoring.seen import SeenDecision, SeenIndex, seen_decision
from tests.unit.test_scoring_support import fake_store, make_record  # noqa: F401

pytestmark = pytest.mark.usefixtures("fake_store")

D = dt.date


def test_unshown_candidate_returns_within_grace_and_expires_after(tmp_path: Path) -> None:
    index = SeenIndex(tmp_path / "seen.jsonl")
    rec = make_record("A fictional unshown paper", None, kind=WorkKind.PREPRINT)
    index.record_run(rec, ["arxiv:2510.00001"], "2026-10-10-daily", shown=False, shown_no=None)
    keys = ["arxiv:2510.00001"]
    assert not index.is_blocked(rec, today=D(2026, 10, 12), grace_days=7, keys=keys)
    assert not index.is_blocked(rec, today=D(2026, 10, 17), grace_days=7, keys=keys)
    assert index.is_blocked(rec, today=D(2026, 10, 18), grace_days=7, keys=keys)
    assert index.expire(today=D(2026, 10, 18), grace_days=7) == 1
    assert index.lookup(keys) is not None and index.lookup(keys).expired  # type: ignore[union-attr]
    # a later run within grace keeps the first_run clock (seen != shown)
    index2 = SeenIndex(tmp_path / "seen2.jsonl")
    index2.record_run(rec, keys, "2026-10-10-daily", shown=False, shown_no=None)
    index2.record_run(rec, keys, "2026-10-14-daily", shown=False, shown_no=None)
    entry = index2.lookup(keys)
    assert entry is not None and entry.first_run == "2026-10-10-daily"
    assert entry.last_run == "2026-10-14-daily" and not entry.shown


def test_shown_blocks_and_journal_version_returns_once(tmp_path: Path) -> None:
    index = SeenIndex(tmp_path / "seen.jsonl")
    preprint = make_record("A fictional paper", None, kind=WorkKind.PREPRINT)
    keys = ["arxiv:2510.00002"]
    index.record_run(preprint, keys, "2026-10-01-daily", shown=True, shown_no=3)
    assert index.is_blocked(preprint, today=D(2026, 10, 2), grace_days=7, keys=keys)
    journal = make_record("A fictional paper", None, kind=WorkKind.ARTICLE, doi="10.0000/j.2")
    jkeys = ["doi:10.0000/j.2", "arxiv:2510.00002"]
    assert not index.is_blocked(journal, today=D(2026, 11, 1), grace_days=7, keys=jkeys)
    index.record_run(journal, jkeys, "2026-11-01-daily", shown=True, shown_no=1)
    entry = index.lookup(jkeys)
    assert entry is not None and entry.journal_version_note and entry.kind is WorkKind.ARTICLE
    assert "doi:10.0000/j.2" in entry.keys
    # second time: blocked
    assert index.is_blocked(journal, today=D(2026, 11, 8), grace_days=7, keys=jkeys)


def test_seen_decision_table() -> None:
    today = D(2026, 10, 18)
    assert seen_decision(None, WorkKind.PREPRINT, today=today, grace_days=7) is SeenDecision.NEW
    unshown = SeenEntry(keys=["k"], first_run="2026-10-15-daily", last_run="2026-10-15-daily")
    assert seen_decision(unshown, WorkKind.PREPRINT, today=today, grace_days=7) is (
        SeenDecision.GRACE
    )
    assert seen_decision(unshown, WorkKind.PREPRINT, today=today, grace_days=2) is (
        SeenDecision.EXPIRED
    )
    shown = SeenEntry(
        keys=["k"],
        first_run="2026-10-01-weekly",
        last_run="2026-10-01-weekly",
        shown=True,
        shown_run="2026-10-01-weekly",
        shown_no=1,
        kind=WorkKind.PREPRINT,
    )
    assert seen_decision(shown, WorkKind.PREPRINT, today=today, grace_days=7) is (
        SeenDecision.SHOWN
    )
    assert seen_decision(shown, WorkKind.ARTICLE, today=today, grace_days=7) is (
        SeenDecision.JOURNAL_VERSION
    )
    shown.journal_version_note = "seen"
    assert seen_decision(shown, WorkKind.ARTICLE, today=today, grace_days=7) is SeenDecision.SHOWN


def test_save_and_load_round_trip_validates_schema(tmp_path: Path) -> None:
    path = tmp_path / "state" / "seen.jsonl"
    index = SeenIndex(path)
    rec = make_record("Title " * 30, None, kind=WorkKind.PREPRINT)
    index.record_run(rec, ["arxiv:2510.00003"], "2026-10-18-daily", shown=True, shown_no=2)
    index.record_run(
        make_record("Other", None),
        ["doi:10.0000/o"],
        "2026-10-18-daily",
        shown=False,
        shown_no=None,
    )
    index.save()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["shown"] is True and first["shown_no"] == 2 and first["kind"] == "preprint"
    assert len(first["title_short"]) <= 60
    loaded = SeenIndex.load(path)
    assert len(loaded) == 2
    assert loaded.lookup(["doi:10.0000/o"]) is not None
    assert loaded.mapping()["arxiv:2510.00003"].shown_run == "2026-10-18-daily"


def test_load_missing_file_is_empty(tmp_path: Path) -> None:
    index = SeenIndex.load(tmp_path / "missing.jsonl")
    assert len(index) == 0 and index.lookup(["x"]) is None
