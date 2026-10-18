"""process.extract / process.ocr: text extraction, OCR decision, alias redirect."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kallimachos.interfaces import UsageError
from kallimachos.paths import DataLayout
from kallimachos.process import extract, ocr
from tests.unit.conftest_pdf import (  # noqa: F401
    EID_A,
    fake_runner,
    fake_store,
    layout_fixture,
    make_pdf,
)

GOOD_PAGE = ("The fictional lattice theorem holds for every parameter. " * 6).strip()


def test_needs_ocr_thresholds() -> None:
    assert extract.needs_ocr([]) is True
    assert extract.needs_ocr(["short"]) is True  # < 200 chars per page
    assert extract.needs_ocr([GOOD_PAGE, GOOD_PAGE]) is False
    digits = ("1234567890 " * 30).strip()  # alpha ratio 0
    assert extract.needs_ocr([digits]) is True
    spaced = " ".join(
        [
            "t",
            "h",
            "e",
            "f",
            "i",
            "c",
            "t",
            "i",
            "o",
            "n",
            "a",
            "l",
            "t",
            "h",
            "e",
            "o",
            "r",
            "e",
            "m",
        ]
        * 20
    )  # > 200 tokens
    assert extract.needs_ocr([spaced]) is True
    few_spaced = "a b c " + GOOD_PAGE  # spaced ratio counts only above 200 tokens
    assert extract.needs_ocr([few_spaced]) is False


def test_text_stats() -> None:
    chars, alpha, spaced, tokens = extract.text_stats(["ab 12", "c"])
    assert (chars, tokens) == (5, 3)
    assert alpha == pytest.approx(3 / 5)
    assert spaced == pytest.approx(1 / 3)


def test_split_pages_drops_trailing_form_feed() -> None:
    assert extract.split_pages("a\fb\f") == ["a", "b"]
    assert extract.split_pages("a\f\f") == ["a", ""]
    assert extract.split_pages("") == []


def test_extract_with_fake_runner_writes_text_and_meta(layout: DataLayout, tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "work.pdf", [GOOD_PAGE, GOOD_PAGE + "\nReferences"])
    runner = fake_runner([GOOD_PAGE, GOOD_PAGE + "\nReferences"])
    meta = extract.extract_text(pdf, layout, runner=runner)
    assert runner.calls[0][:2] == ["pdftotext", "-layout"]
    assert meta.eid == extract.eid_for(pdf)
    assert meta.pages == 2
    assert meta.ocr == "none"
    assert extract.load_pages(layout, meta.eid) == [GOOD_PAGE, GOOD_PAGE + "\nReferences"]
    stored = extract.load_meta(layout, meta.eid)
    assert stored is not None
    assert stored.chars == meta.chars > 0
    assert (layout.text_dir / f"{meta.eid}.meta.json").is_file()


def test_extract_marks_ocr_needed(layout: DataLayout, tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "scan.pdf", ["", ""])
    meta = extract.extract_text(pdf, layout, runner=fake_runner(["", " "]))
    assert meta.ocr == "needed"
    assert meta.pages == 2


def test_extract_missing_pdf_is_usage_error(layout: DataLayout, tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        extract.extract_text(tmp_path / "missing.pdf", layout, runner=fake_runner([]))


def test_missing_tool_gives_doctor_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(extract.ToolMissing, match="doctor"):
        extract.require_tool("pdftotext", hint="install poppler")
    assert ocr.ocr_available() is False


def test_load_pages_follows_alias(layout: DataLayout) -> None:
    from kallimachos.state import store

    old, new = "aaaaaaaaaaaa", "bbbbbbbbbbbb"
    store.write_text_atomic(layout.text_dir / f"{new}.txt", "p1\fp2")
    store.write_json_atomic(layout.alias_file, {old: new})
    assert extract.load_pages(layout, old) == ["p1", "p2"]
    assert extract.resolve_alias(layout, new) == new
    with pytest.raises(UsageError, match="process extract"):
        extract.load_pages(layout, "cccccccccccc")


def test_alias_cycle_is_cut(layout: DataLayout) -> None:
    from kallimachos.state import store

    store.write_json_atomic(
        layout.alias_file, {"aaaaaaaaaaaa": "bbbbbbbbbbbb", "bbbbbbbbbbbb": "aaaaaaaaaaaa"}
    )
    assert extract.resolve_alias(layout, "aaaaaaaaaaaa") == "bbbbbbbbbbbb"


def test_ocr_command_lines(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(cmd: list[str]) -> str:
        calls.append(list(cmd))
        return ""

    out = tmp_path / "ocr" / "x.pdf"
    ocr.ocr_pdf(tmp_path / "in.pdf", out, langs=["eng", "deu"], runner=runner)
    ocr.ocr_pdf(tmp_path / "in.pdf", out, langs=["eng"], force=True, runner=runner)
    assert calls[0][:4] == ["ocrmypdf", "--skip-text", "-l", "eng+deu"]
    assert calls[1][:2] == ["ocrmypdf", "--force-ocr"]
    assert calls[0][-1] == str(out)
    assert out.parent.is_dir()


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="poppler not installed")
def test_extract_real_pdftotext(layout: DataLayout, tmp_path: Path) -> None:
    pdf = make_pdf(
        tmp_path / "real.pdf", [["Theorem 1.1. The fictional statement holds."], ["References"]]
    )
    meta = extract.extract_text(pdf, layout)
    pages = extract.load_pages(layout, meta.eid)
    assert meta.pages == 2
    assert "fictional statement holds" in pages[0]
    assert "References" in pages[1]
