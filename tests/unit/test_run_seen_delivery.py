"""seen = shown only when a channel delivered the message; ``--rerun`` forgets the run's own
seen entries (regression tests for a verification finding)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kallimachos.interfaces import ExitCode
from kallimachos.paths import DataLayout
from kallimachos.scoring.seen import SeenIndex
from tests.unit.test_integration_support import (
    ANSWERS,
    jsonl,
    recorded_http,
    run_cli,
    source_fixtures,
)

DAY = "2026-10-13"
RID = f"{DAY}-daily"


def _setup(home: Path, monkeypatch: pytest.MonkeyPatch) -> DataLayout:
    recorded_http(monkeypatch, *source_fixtures())
    run_cli("init", "--answers", str(ANSWERS))
    run_cli("profile", "approve")
    return DataLayout(home)


def test_no_notify_leaves_candidates_unshown(
    _isolated_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = _setup(_isolated_home, monkeypatch)
    run_cli("run", "daily", "--date", DAY, "--first-run", "--no-notify")
    seen = jsonl(layout.seen_file)
    assert seen, "the run recorded its candidates"
    assert not any(r["shown"] for r in seen), "no message went out, nothing may count as shown"
    assert all(r["first_run"] == RID and r["shown_run"] is None for r in seen)


def test_unapproved_markdown_then_rerun_keeps_candidates(
    _isolated_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = _setup(_isolated_home, monkeypatch)
    layout.daily.mkdir(parents=True, exist_ok=True)
    (layout.daily / f"{DAY}.md").write_text("my own notes\n", encoding="utf-8")
    code = run_cli("run", "daily", "--date", DAY, "--first-run", expect=None)
    assert code == int(ExitCode.APPROVAL_REQUIRED)
    assert layout.run_file(RID).exists()
    seen = jsonl(layout.seen_file)
    assert seen and not any(r["shown"] for r in seen)

    run_cli("--yes", "run", "daily", "--date", DAY, "--first-run", "--rerun")
    run = json.loads(layout.run_file(RID).read_text(encoding="utf-8"))
    assert run["candidates"], "rerun after approval lost the candidates (blocked as seen)"
    assert "seen" not in run["dropped"]
    seen2 = jsonl(layout.seen_file)
    selection = json.loads(layout.selection_file(RID).read_text(encoding="utf-8"))
    assert sum(1 for r in seen2 if r["shown"]) == len(selection["message_no"])
    assert all(r["shown_run"] == RID for r in seen2 if r["shown"])
    # the user's own text survived the approved write
    assert "my own notes" in (layout.daily / f"{DAY}.md").read_text(encoding="utf-8")


def test_failing_channel_does_not_mark_shown(
    _isolated_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from kallimachos.interfaces import NotifyResult
    from kallimachos.notify import markdown_daily

    layout = _setup(_isolated_home, monkeypatch)

    def failing(self: object, message: object) -> NotifyResult:
        return NotifyResult(channel="markdown", ok=False, error="disk full")

    monkeypatch.setattr(markdown_daily.MarkdownDailyNotifier, "send", failing)
    code = run_cli("run", "daily", "--date", DAY, "--first-run", expect=None)
    assert code == int(ExitCode.PARTIAL)
    seen = jsonl(layout.seen_file)
    assert seen and not any(r["shown"] for r in seen)


def test_undo_run_drops_own_entries_and_unshows_older_ones(tmp_path: Path) -> None:
    from kallimachos.interfaces import SeenEntry, WorkKind

    index = SeenIndex(tmp_path / "seen.jsonl")
    index._add(
        SeenEntry(
            keys=["doi:10.0000/a"],
            first_run="2026-10-01-daily",
            last_run="2026-10-13-daily",
            shown=True,
            shown_run="2026-10-13-daily",
            shown_no=1,
            kind=WorkKind.PREPRINT,
            title_short="older entry shown by the run",
        )
    )
    index._add(
        SeenEntry(
            keys=["doi:10.0000/b"],
            first_run="2026-10-13-daily",
            last_run="2026-10-13-daily",
            shown=True,
            shown_run="2026-10-13-daily",
            shown_no=2,
            kind=WorkKind.PREPRINT,
            title_short="introduced by the run",
        )
    )
    index._add(
        SeenEntry(
            keys=["doi:10.0000/c"],
            first_run="2026-10-01-daily",
            last_run="2026-10-01-daily",
            shown=True,
            shown_run="2026-10-01-daily",
            shown_no=1,
            kind=WorkKind.PREPRINT,
            title_short="untouched",
        )
    )
    assert index.undo_run("2026-10-13-daily") == 2
    entries = {e.keys[0]: e for e in index.entries}
    assert set(entries) == {"doi:10.0000/a", "doi:10.0000/c"}
    assert entries["doi:10.0000/a"].shown is False
    assert entries["doi:10.0000/a"].shown_run is None
    assert entries["doi:10.0000/c"].shown is True
    assert index.lookup(["doi:10.0000/b"]) is None
    assert index.undo_run("2026-10-13-daily") == 0


# ---------------------------------------------------------------------------
# agent / Hermes delivery: ``run --no-notify`` + ``select`` marks the selection shown
# ---------------------------------------------------------------------------


def _agent_select(layout: DataLayout) -> tuple[list[str], list[str]]:
    """``run --no-notify`` then ``select``: certain-pass (or the first) candidates as main, one more
    as near miss; at least one candidate stays unselected."""
    run_cli("run", "daily", "--date", DAY, "--first-run", "--no-notify")
    run = json.loads(layout.run_file(RID).read_text(encoding="utf-8"))
    ids = [c["id"] for c in run["candidates"]]
    assert len(ids) >= 3, "the fixtures must give a selected and an unselected candidate"
    certain = [c["id"] for c in run["candidates"] if c.get("certain_pass")]
    main_ids = certain or ids[:1]
    near_ids = [i for i in ids if i not in main_ids][:1]
    run_cli("select", "--run", RID, "--main", ",".join(main_ids), "--near", ",".join(near_ids))
    selection = json.loads(layout.selection_file(RID).read_text(encoding="utf-8"))
    selected = list(selection["message_no"].values())
    unselected = [i for i in ids if i not in selected]
    assert unselected
    return selected, unselected


def _entry_for(layout: DataLayout, run: dict[str, object], cid: str) -> dict[str, object]:
    cands = {c["id"]: c for c in run["candidates"]}  # type: ignore[attr-defined]
    keys = set(cands[cid]["keys"] or [cid])
    hits = [r for r in jsonl(layout.seen_file) if keys & set(r["keys"])]
    assert len(hits) == 1, f"{cid}: expected one seen entry, got {len(hits)}"
    return hits[0]


def test_select_marks_selection_shown_and_next_run_skips_it(
    _isolated_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = _setup(_isolated_home, monkeypatch)
    selected, unselected = _agent_select(layout)
    run = json.loads(layout.run_file(RID).read_text(encoding="utf-8"))
    selection = json.loads(layout.selection_file(RID).read_text(encoding="utf-8"))
    for no, cid in selection["message_no"].items():
        entry = _entry_for(layout, run, cid)
        assert entry["shown"] is True and entry["shown_run"] == RID
        assert entry["shown_no"] == (int(no) if no.isdigit() else None)
    for cid in unselected:
        entry = _entry_for(layout, run, cid)
        assert entry["shown"] is False and entry["shown_run"] is None

    next_day = "2026-10-14"
    run_cli("run", "daily", "--date", next_day, "--first-run", "--no-notify")
    nxt = json.loads(layout.run_file(f"{next_day}-daily").read_text(encoding="utf-8"))
    next_ids = {c["id"] for c in nxt["candidates"]}
    assert not next_ids & set(selected), "a selected (delivered) paper came back the next day"
    assert set(unselected) <= next_ids, "unselected papers must compete again within grace"


def test_select_twice_keeps_one_seen_entry(
    _isolated_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = _setup(_isolated_home, monkeypatch)
    _agent_select(layout)
    before = jsonl(layout.seen_file)
    selection = json.loads(layout.selection_file(RID).read_text(encoding="utf-8"))
    main_ids = selection["main"]
    near_ids = selection.get("near", [])
    args = ["select", "--run", RID, "--main", ",".join(main_ids)]
    if near_ids:
        args += ["--near", ",".join(near_ids)]
    run_cli(*args)
    after = jsonl(layout.seen_file)
    assert after == before
    run = json.loads(layout.run_file(RID).read_text(encoding="utf-8"))
    for cid in selection["message_no"].values():
        _entry_for(layout, run, cid)
