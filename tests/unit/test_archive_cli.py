"""``archive run|retry|status|scan-existing`` handlers with an injected context."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import pytest

from kallimachos import cli_archive
from kallimachos.archive import download
from kallimachos.cli import build_parser
from kallimachos.interfaces import (
    Candidate,
    FeedbackEntry,
    FeedbackTag,
    RunKind,
    RunResult,
    UsageError,
)
from tests.unit.test_archive_pipeline import ARXIV_ID, ARXIV_PDF, make_ctx
from tests.unit.test_archive_support import FakeHttp, make_pdf, store_impl

__all__ = ["store_impl"]


@pytest.fixture(autouse=True)
def _pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download, "pdf_pages", lambda path: 2)


def ns(*argv: str) -> argparse.Namespace:
    return build_parser().parse_args(list(argv))


def test_cmd_run_from_feedback_and_manual(
    tmp_path: Path, store_impl: None, capsys: pytest.CaptureFixture[str]
) -> None:
    http = FakeHttp()
    http.add(ARXIV_PDF, make_pdf())
    at = dt.datetime(2026, 10, 18, tzinfo=dt.UTC)
    ctx = make_ctx(
        tmp_path,
        http=http,
        feedback=[
            FeedbackEntry(
                at=at,
                run_id="2026-10-18-daily",
                candidate_id=f"arXiv:{ARXIV_ID}",
                tag=FeedbackTag.READ,
            )
        ],
        runs=[],
    )
    ctx.layout.archive.mkdir(parents=True)
    ctx.layout.manual_file.write_text("bogus-line\n", encoding="utf-8")
    code = cli_archive.cmd_run(ns("--json", "archive", "run"), context=ctx)
    out = json.loads(capsys.readouterr().out)
    assert out["stored"] == 1 and out["summary"] == "archive: 1 stored"
    assert code == 3  # manual.txt problem reported, result still written
    assert any("bogus-line" in e for e in out["errors"])


def test_cmd_run_dry_run(
    tmp_path: Path, store_impl: None, capsys: pytest.CaptureFixture[str]
) -> None:
    http = FakeHttp()
    run = RunResult(
        run_id="2026-10-18-daily",
        kind=RunKind.DAILY,
        candidates=[Candidate(no=1, id=f"arXiv:{ARXIV_ID}", title="t", certain_pass=True)],
    )
    ctx = make_ctx(tmp_path, http=http, feedback=[], runs=[run])
    code = cli_archive.cmd_run(ns("--dry-run", "archive", "run"), context=ctx)
    text = capsys.readouterr().out
    assert (
        code == 0
        and "nothing was written" in text
        and f"auto_pass:2026-10-18-daily arxiv:{ARXIV_ID}" in text
    )
    assert http.calls == [] and not (tmp_path / "home").exists()


def test_cmd_run_requires_enabled_and_root(tmp_path: Path, store_impl: None) -> None:
    ctx = make_ctx(tmp_path, feedback=[], runs=[])
    ctx.config.archive.enabled = False
    with pytest.raises(UsageError):
        cli_archive.cmd_run(ns("archive", "run"), context=ctx)
    ctx.config.archive.enabled = True
    ctx.config.archive.root = None
    with pytest.raises(UsageError):
        cli_archive.cmd_run(ns("archive", "run"), context=ctx)


def test_cmd_status_and_retry(
    tmp_path: Path, store_impl: None, capsys: pytest.CaptureFixture[str]
) -> None:
    http = FakeHttp()
    http.add(ARXIV_PDF, make_pdf())
    ctx = make_ctx(tmp_path, http=http, feedback=[], runs=[])
    ctx.layout.archive.mkdir(parents=True)
    ctx.layout.manual_file.write_text(f"{ARXIV_ID}\n", encoding="utf-8")
    cli_archive.cmd_run(ns("archive", "run"), context=ctx)
    capsys.readouterr()
    assert cli_archive.cmd_status(ns("--json", "archive", "status"), context=ctx) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["catalog"] == {"stored": 1}
    assert status["last_log"]["command"] == "archive run"
    assert cli_archive.cmd_retry(ns("archive", "retry"), context=ctx) == 0
    assert "retried 0" in capsys.readouterr().out


def test_cmd_scan_existing(
    tmp_path: Path, store_impl: None, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    (root / "2501.01234.pdf").write_bytes(make_pdf())
    ctx = make_ctx(tmp_path, sources={})
    code = cli_archive.cmd_scan_existing(ns("archive", "scan-existing", str(root)), context=ctx)
    assert code == 0
    assert "read-only" in capsys.readouterr().out
    assert ctx.layout.existing_catalog_file.exists()
    with pytest.raises(UsageError):
        cli_archive.cmd_scan_existing(
            ns("archive", "scan-existing", str(root / "missing")), context=ctx
        )


def test_confirm_rejects_an_unknown_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``archive confirm`` is implemented: an unknown sha is a usage error, not a stub."""
    monkeypatch.setenv("KALLIMACHOS_HOME", str(tmp_path))
    with pytest.raises(UsageError):
        cli_archive.cmd_confirm(
            ns("--data-dir", str(tmp_path), "archive", "confirm", "--sha", "0" * 64)
        )


def test_load_recent_runs_skips_selection_files(tmp_path: Path, store_impl: None) -> None:
    from kallimachos.interfaces import dump
    from kallimachos.paths import DataLayout

    layout = DataLayout(tmp_path)
    layout.runs.mkdir(parents=True)
    today = dt.date.today()
    rid = f"{today.isoformat()}-daily"
    (layout.runs / f"{rid}.json").write_text(json.dumps(dump(RunResult(run_id=rid, date=today))))
    (layout.runs / f"{rid}.selection.json").write_text("{}")
    (layout.runs / "2000-01-01-daily.json").write_text(
        json.dumps(dump(RunResult(run_id="2000-01-01-daily")))
    )
    runs = cli_archive.load_recent_runs(layout)
    assert [r.run_id for r in runs] == [rid]


def test_cmd_test_probes_one_oa_source_and_the_root(
    tmp_path: Path, store_impl: None, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.unit.test_sources_fakes import fixture

    http = FakeHttp()
    http.add("https://export.arxiv.org/", fixture("arxiv", "empty").body)
    ctx = make_ctx(tmp_path, http=http)
    ctx.config.sources.arxiv.enabled = True
    Path(ctx.config.archive.root or "").mkdir(parents=True, exist_ok=True)
    assert cli_archive.cmd_test(ns("--json", "archive", "test"), context=ctx) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "arxiv" and payload["ok"] and payload["archive_root_ok"]
    ctx.config.archive.root = None
    assert cli_archive.cmd_test(ns("archive", "test"), context=ctx) == 3
    assert "archive.root is not set" in capsys.readouterr().out
    for name in ("arxiv", "openalex", "crossref", "iacr_eprint"):
        getattr(ctx.config.sources, name).enabled = False
    with pytest.raises(UsageError):
        cli_archive.cmd_test(ns("archive", "test"), context=ctx)
