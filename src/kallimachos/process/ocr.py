"""OCR via ``ocrmypdf``. Output ``ocr/<eid>.pdf``; the archive PDF never changes."""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

from kallimachos.process.extract import Runner, _run, require_tool

OCR_TOOL = "ocrmypdf"


def ocr_available() -> bool:
    return shutil.which(OCR_TOOL) is not None


def ocr_command(pdf: Path, out: Path, *, langs: Sequence[str], force: bool, exe: str) -> list[str]:
    mode = "--force-ocr" if force else "--skip-text"
    cmd = [exe, mode]
    if langs:
        cmd += ["-l", "+".join(langs)]
    cmd += [str(pdf), str(out)]
    return cmd


def ocr_pdf(
    pdf: Path,
    out: Path,
    *,
    langs: Sequence[str],
    force: bool = False,
    runner: Runner | None = None,
) -> Path:
    """``ocrmypdf --skip-text``; when still unreadable the caller retries with ``force``
    (``--force-ocr``). Writes ``out`` (normally ``ocr/<eid>.pdf``) and returns it."""
    if runner is None:
        exe = require_tool(OCR_TOOL, hint="pip install ocrmypdf, plus tesseract language packs")
        runner = _run
    else:
        exe = OCR_TOOL
    out.parent.mkdir(parents=True, exist_ok=True)
    runner(ocr_command(pdf, out, langs=langs, force=force, exe=exe))
    return out
