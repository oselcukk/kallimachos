"""Integration (ii): ``archive run`` end to end with the real context builder, the real arXiv source
on recorded fixtures and the real catalog: ``manual.txt`` -> metadata -> OA PDF -> stored file ->
``catalog.jsonl`` row -> ``archive status``. No network, no fakes besides the HTTP transport.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from kallimachos.archive import download
from kallimachos.archive.catalog import Catalog
from kallimachos.interfaces import CatalogEventType, CatalogStatus, ExitCode, HttpResponse
from kallimachos.paths import DataLayout
from tests.unit.test_archive_support import make_pdf
from tests.unit.test_integration_support import (
    ANSWERS,
    edit_config,
    jsonl,
    recorded_http,
    run_cli,
)
from tests.unit.test_sources_fakes import fixture

ARXIV_ID = "2610.00002"  # the work of tests/fixtures/arxiv/id_list (fictional)


def test_manual_trigger_downloads_and_catalogs(
    _isolated_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    layout = DataLayout(_isolated_home)
    pdf = make_pdf("fictional toy models")
    http = recorded_http(
        monkeypatch,
        ("https://export.arxiv.org/", fixture("arxiv", "id_list")),
        ("http://export.arxiv.org/", fixture("arxiv", "id_list")),
        (
            f"https://arxiv.org/pdf/{ARXIV_ID}",
            HttpResponse(url="", status=200, headers={"Content-Type": "application/pdf"}, body=pdf),
        ),
    )
    monkeypatch.setattr(download, "pdf_pages", lambda path: 2)  # pdfinfo may be missing

    run_cli("init", "--answers", str(ANSWERS))
    run_cli("profile", "approve")
    root = tmp_path / "library"
    root.mkdir()
    edit_config(layout, archive__enabled=True, archive__root=str(root))
    layout.archive.mkdir(parents=True, exist_ok=True)
    # "modular forms" (plural) is not the whole-phrase core term "modular form": without the
    # hint the work would land in _Unsorted, so the request names the channel
    layout.manual_file.write_text(
        f"arXiv:{ARXIV_ID}v2 channel=B  # fictional request\n", encoding="utf-8"
    )
    capsys.readouterr()

    assert run_cli("--json", "archive", "run", expect=None) in (ExitCode.OK, ExitCode.PARTIAL)
    report = json.loads(capsys.readouterr().out)
    assert report["stored"] == 1, report
    assert report["errors"] == []
    (rel,) = report["stored_paths"]
    stored = root / rel
    assert stored.read_bytes() == pdf
    assert rel.endswith(f"_arXiv-{ARXIV_ID}v2.pdf") and "Toy-Models" in rel
    assert rel.split("/")[0].startswith("B-")  # the channel folder of the Q&A profile

    sha = hashlib.sha256(pdf).hexdigest()
    catalog = Catalog.load(layout.catalog_file)
    (entry,) = catalog.entries()
    assert entry.id == sha[:12] and entry.sha256 == sha
    assert entry.arxiv == ARXIV_ID and entry.arxiv_version == "v2"
    assert entry.status is CatalogStatus.STORED and entry.channel == "B"
    assert entry.path == rel and entry.filename == stored.name
    assert [e.type for e in entry.events] == [CatalogEventType.STORED]
    assert f"arxiv:{ARXIV_ID}" in entry.keys
    log = jsonl(layout.archive_log_file)
    assert log[-1]["counts"]["stored"] == 1
    assert not layout.archive_tmp.exists() or not any(layout.archive_tmp.iterdir())

    # the real client paced arXiv and never sent a secret; the PDF came from arxiv.org
    urls = [u for _m, u, _h in http.calls]
    assert any(u.startswith("https://export.arxiv.org/api/query") for u in urls)
    assert any(u.startswith(f"https://arxiv.org/pdf/{ARXIV_ID}") for u in urls)
    assert all("Authorization" not in h for _m, _u, h in http.calls)

    # a second run sees the key in the catalog: nothing downloaded again
    calls = len(http.calls)
    run_cli("--json", "archive", "run")
    again = json.loads(capsys.readouterr().out)
    assert again["stored"] == 0 and again["skipped_seen"] == 1
    assert len(http.calls) == calls
    run_cli("archive", "status")
    assert "1" in capsys.readouterr().out
