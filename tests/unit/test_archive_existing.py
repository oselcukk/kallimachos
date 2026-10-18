"""``archive scan-existing``: read-only, privacy gate, dataless, duplicates."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from kallimachos.archive import existing
from kallimachos.archive.existing import (
    filename_idents,
    privacy_gate,
    scan_existing,
    text_idents,
)
from kallimachos.archive.rows import ExistingEntry
from kallimachos.config.schema import Config
from kallimachos.interfaces import Record, WorkKind
from kallimachos.paths import DataLayout
from tests.unit.test_archive_support import FakeHttp, FakeSource, make_pdf, store_impl

__all__ = ["store_impl"]


def snapshot(root: Path) -> dict[str, tuple[int, float, str]]:
    out = {}
    for p in sorted(root.rglob("*")):
        st = os.lstat(p)
        digest = (
            hashlib.sha256(p.read_bytes()).hexdigest()
            if p.is_file() and os.access(p, os.R_OK)
            else ""
        )
        out[p.relative_to(root).as_posix()] = (st.st_mode, st.st_mtime, digest)
    return out


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "Library"
    (root / "Papers").mkdir(parents=True)
    (root / "Private").mkdir()
    (root / "Teaching" / "lecture-notes").mkdir(parents=True)
    (root / "Papers" / "2501.01234v2.pdf").write_bytes(make_pdf("Fictional preprint"))
    (root / "Papers" / "copy-of-preprint.pdf").write_bytes(make_pdf("Fictional preprint"))
    (root / "Papers" / "s00000-000-00000-0.pdf").write_bytes(make_pdf("Springer style"))
    (root / "Papers" / "unknown.pdf").write_bytes(make_pdf("No identifier here"))
    secret = root / "Private" / "passport-scan.pdf"
    secret.write_bytes(make_pdf("doi 10.0000/secret.1"))
    secret.chmod(0)
    (root / "Teaching" / "lecture-notes" / "10.0000_fict.5.pdf").write_bytes(make_pdf("notes"))
    (root / "notes.txt").write_text("not a pdf", encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _restore_perms(library: Path) -> object:
    yield
    for p in library.rglob("*"):
        p.chmod(0o644 if p.is_file() else 0o755)


def test_filename_patterns() -> None:
    assert filename_idents("2501.01234v2.pdf") == {"arxiv": "2501.01234", "arxiv_version": "v2"}
    assert filename_idents("math.GT_0501234v1.pdf")["arxiv"] == "math.GT/0501234"
    assert filename_idents("s00000-000-00000-0.pdf")["doi"] == "10.1007/s00000-000-00000-0"
    assert filename_idents("978-3-000-00000-0_1.pdf")["doi"] == "10.1007/978-3-000-00000-0_1"
    assert filename_idents("PhysRevD.000.000000.pdf")["doi"] == "10.1103/physrevd.000.000000"
    assert filename_idents("1-s2.0-S0021869317301234-main.pdf")["pii"] == "S0021869317301234"
    assert filename_idents("10.0000_fict.5.pdf")["doi"] == "10.0000/fict.5"
    assert filename_idents("holiday-photos.pdf") == {}


def test_text_idents_ignore_references() -> None:
    text = "Title\narXiv:2501.01234v3\nIntro\nReferences\n[1] doi 10.0000/cited.9\n"
    assert text_idents(text) == {"arxiv": "2501.01234", "arxiv_version": "v3"}
    assert text_idents("see https://doi.org/10.0000/own.1 here")["doi"] == "10.0000/own.1"


def test_privacy_gate_default_refuses() -> None:
    sens = Config().archive.sensitive_path_regex
    ok = ExistingEntry(
        path="Papers/x.pdf",
        root="/r",
        size=1,
        mtime=0,
        doi="10.0000/x",
        kind_guess=WorkKind.ARTICLE,
    )
    assert privacy_gate(ok, sens) == (True, "")
    assert not privacy_gate(ExistingEntry(path="Papers/x.pdf", root="/r", size=1, mtime=0), sens)[0]
    for path in [
        "Students/x.pdf",
        "exam-2024/x.pdf",
        "Health/x.pdf",
        "Öğrenci/x.pdf",
        "Courses/slides.pdf",
    ]:
        e = ExistingEntry(
            path=path, root="/r", size=1, mtime=0, doi="10.0000/x", kind_guess=WorkKind.ARTICLE
        )
        assert not privacy_gate(e, sens)[0], path
    odd = ExistingEntry(path="Papers/x.pdf", root="/r", size=1, mtime=0, doi="10.0000/x")
    assert not privacy_gate(odd, sens)[0]  # unknown kind


def test_scan_is_read_only_and_private(library: Path, tmp_path: Path, store_impl: None) -> None:
    layout = DataLayout(tmp_path / "home")
    before = snapshot(library)
    crossref = FakeSource("crossref", records={})
    arxiv = FakeSource(
        "arxiv", records={"arxiv:2501.01234": Record(title="Looked Up Fictional Title")}
    )
    http = FakeHttp()
    report = scan_existing(
        library, layout, Config(), http, sources={"crossref": crossref, "arxiv": arxiv}
    )
    assert snapshot(library) == before  # nothing created, renamed, moved or touched
    assert report.skipped_sensitive == 1
    assert report.scanned == 5
    rows = [json.loads(x) for x in layout.existing_catalog_file.read_text().splitlines()]
    paths = {r["path"] for r in rows}
    assert "Private/passport-scan.pdf" not in paths  # never recorded
    assert not any("secret" in json.dumps(r) for r in rows)
    by_path = {r["path"]: r for r in rows}
    pre = by_path["Papers/2501.01234v2.pdf"]
    assert pre["arxiv"] == "2501.01234" and pre["arxiv_version"] == "v2"
    assert pre["queried"] is True and pre["title"] == "Looked Up Fictional Title"
    # lecture notes have a DOI-like file name but fail the gate: no request
    notes = by_path["Teaching/lecture-notes/10.0000_fict.5.pdf"]
    assert notes["doi"] == "10.0000/fict.5" and notes["queried"] is False
    assert "doi:10.0000/fict.5" not in crossref.metadata_calls
    assert by_path["Papers/unknown.pdf"]["queried"] is False
    assert crossref.metadata_calls == ["doi:10.1007/s00000-000-00000-0"]
    assert (
        http.calls == []
    )  # sources go through their own http; the scanner itself requests nothing
    assert ("Papers/2501.01234v2.pdf", "Papers/copy-of-preprint.pdf") in report.duplicates or (
        "Papers/copy-of-preprint.pdf",
        "Papers/2501.01234v2.pdf",
    ) in report.duplicates
    dup_md = (layout.archive / "duplicates.md").read_text()
    assert "copy-of-preprint.pdf" in dup_md and "nothing was moved" in dup_md


def test_scan_without_sources_makes_no_lookup(
    library: Path, tmp_path: Path, store_impl: None
) -> None:
    layout = DataLayout(tmp_path / "home")
    report = scan_existing(library, layout, Config(), FakeHttp())
    assert report.queried == 0


def test_scan_dry_run_writes_nothing(library: Path, tmp_path: Path, store_impl: None) -> None:
    layout = DataLayout(tmp_path / "home")
    report = scan_existing(library, layout, Config(), FakeHttp(), dry_run=True)
    assert report.scanned == 5
    assert not (tmp_path / "home").exists()


def test_scan_incremental_cache(
    library: Path, tmp_path: Path, store_impl: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = DataLayout(tmp_path / "home")
    scan_existing(library, layout, Config(), FakeHttp())
    calls: list[Path] = []
    real = existing._identify

    def counting(path: Path, *a: object) -> ExistingEntry:
        calls.append(path)
        return real(path, *a)  # type: ignore[arg-type]

    monkeypatch.setattr(existing, "_identify", counting)
    report = scan_existing(library, layout, Config(), FakeHttp())
    assert calls == [] and report.from_cache == 5
    (library / "Papers" / "unknown.pdf").write_bytes(make_pdf("changed content here"))
    scan_existing(library, layout, Config(), FakeHttp())
    assert [p.name for p in calls] == ["unknown.pdf"]


def test_dataless_files_are_not_opened(
    library: Path, tmp_path: Path, store_impl: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = DataLayout(tmp_path / "home")
    monkeypatch.setattr(existing, "is_dataless", lambda p: p.name == "unknown.pdf")
    opened: list[str] = []
    real = existing.sha256_of
    monkeypatch.setattr(existing, "sha256_of", lambda p: opened.append(p.name) or real(p))
    report = scan_existing(library, layout, Config(), FakeHttp())
    assert report.skipped_dataless == 1
    assert "unknown.pdf" not in opened
    rows = [json.loads(x) for x in layout.existing_catalog_file.read_text().splitlines()]
    row = next(r for r in rows if r["path"].endswith("unknown.pdf"))
    assert row["local"] is False and row["sha256"] is None


def test_exclude_and_archive_root_are_skipped(
    library: Path, tmp_path: Path, store_impl: None
) -> None:
    cfg = Config()
    cfg.archive.root = str(library / "Papers")
    cfg.archive.existing_exclude = ["Teaching"]
    report = scan_existing(library, DataLayout(tmp_path / "home"), cfg, FakeHttp())
    assert report.scanned == 0


def test_is_dataless_false_for_regular_file(tmp_path: Path) -> None:
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF")
    assert existing.is_dataless(p) is False


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="pdftotext not installed")
def test_identity_from_first_pages(tmp_path: Path, store_impl: None) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    (root / "scan.pdf").write_bytes(
        make_pdf("Published as doi:10.0000/fromtext.1 in a fictional journal")
    )
    layout = DataLayout(tmp_path / "home")
    scan_existing(root, layout, Config(), FakeHttp())
    row = json.loads(layout.existing_catalog_file.read_text().splitlines()[0])
    assert row["doi"] == "10.0000/fromtext.1" and "text" in row["identified_by"]


def test_default_sensitive_regex_is_word_bounded() -> None:
    """``exam`` hides ``exam-2024`` and ``Exams`` but not ``example`` (lead decision)."""
    patterns = Config().archive.sensitive_path_regex
    assert not existing.is_sensitive("Papers/example.pdf", patterns)
    assert not existing.is_sensitive("Examples/x.pdf", patterns)
    assert existing.is_sensitive("exam-2024/x.pdf", patterns)
    assert existing.is_sensitive("Exams/x.pdf", patterns)
    assert existing.is_sensitive("Students/x.pdf", patterns)
    assert existing.is_sensitive("Öğrenci/x.pdf", patterns)
