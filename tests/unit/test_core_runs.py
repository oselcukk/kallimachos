"""state.runs: run and selection files, last successful run."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from kallimachos.interfaces import (
    Candidate,
    RunKind,
    RunResult,
    SchemaViolation,
    Selection,
    SourceStats,
    UsageError,
)
from kallimachos.paths import DataLayout
from kallimachos.state.runs import (
    last_successful_run_date,
    list_run_ids,
    load_run,
    load_selection,
    run_failed,
    save_run,
    save_selection,
)


def layout(tmp_path: Path) -> DataLayout:
    return DataLayout(tmp_path)


def test_save_and_load_run_round_trip(tmp_path: Path) -> None:
    lay = layout(tmp_path)
    run = RunResult(
        run_id="2026-10-18-daily",
        date=dt.date(2026, 10, 18),
        sources={"arxiv": SourceStats(count=3, requests=1)},
        candidates=[Candidate(no=1, id="arXiv:2510.00001", title="A Fictional Title")],
    )
    save_run(lay, run)
    assert lay.run_file("2026-10-18-daily").exists()
    back = load_run(lay, "2026-10-18-daily")
    assert back == run


def test_save_run_validates(tmp_path: Path) -> None:
    run = RunResult(run_id="2026-10-18-daily")
    run.candidates = [Candidate(no=1, id="x", title="t", authors=[f"A{i}" for i in range(16)])]
    with pytest.raises(SchemaViolation):
        save_run(layout(tmp_path), run)
    assert not layout(tmp_path).run_file("2026-10-18-daily").exists()


def test_load_run_missing_and_malformed(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        load_run(layout(tmp_path), "2026-10-18-daily")
    with pytest.raises(UsageError):
        load_run(layout(tmp_path), "../etc")


def test_selection_round_trip(tmp_path: Path) -> None:
    lay = layout(tmp_path)
    assert load_selection(lay, "2026-10-18-weekly") is None
    sel = Selection(
        run_id="2026-10-18-weekly",
        written_at=dt.datetime(2026, 10, 18, 7, 30, tzinfo=dt.UTC),
        by="model",
        main=["arXiv:2510.00001"],
        message_no={"1": "arXiv:2510.00001"},
    )
    save_selection(lay, sel)
    assert load_selection(lay, "2026-10-18-weekly") == sel


def test_last_successful_run_date(tmp_path: Path) -> None:
    lay = layout(tmp_path)
    assert last_successful_run_date(lay, RunKind.DAILY) is None
    save_run(lay, RunResult(run_id="2026-10-15-daily", sources={"arxiv": SourceStats(count=1)}))
    save_run(
        lay,
        RunResult(
            run_id="2026-10-16-daily",
            errors=["arxiv: 3 tries failed"],
            sources={"arxiv": SourceStats(errors=["x"]), "crossref": SourceStats(count=2)},
        ),
    )  # partial: still successful
    save_run(
        lay,
        RunResult(
            run_id="2026-10-17-daily",
            errors=["all sources failed"],
            sources={"arxiv": SourceStats(errors=["x"])},
        ),
    )  # fatal
    save_run(lay, RunResult(run_id="2026-10-18-weekly"))
    (lay.runs / "2026-10-19-daily.json").write_text("{corrupt", encoding="utf-8")
    save_selection(lay, Selection(run_id="2026-10-15-daily"))
    assert list_run_ids(lay, RunKind.DAILY) == [
        "2026-10-15-daily",
        "2026-10-16-daily",
        "2026-10-17-daily",
        "2026-10-19-daily",
    ]
    assert last_successful_run_date(lay, RunKind.DAILY) == dt.date(2026, 10, 16)
    assert last_successful_run_date(lay, RunKind.WEEKLY) == dt.date(2026, 10, 18)


def test_run_failed_rules() -> None:
    assert not run_failed(RunResult(run_id="2026-10-18-daily"))
    assert run_failed(RunResult(run_id="2026-10-18-daily", errors=["boom"]))


def test_run_file_is_json(tmp_path: Path) -> None:
    lay = layout(tmp_path)
    save_run(lay, RunResult(run_id="2026-10-18-daily"))
    data = json.loads(lay.run_file("2026-10-18-daily").read_text(encoding="utf-8"))
    assert data["schema"] == "kallimachos.run/1"
