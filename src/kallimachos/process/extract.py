"""PDF -> text.

``pdftotext -layout``; pages separated by ``\f``; ``text/<eid>.txt`` + ``text/<eid>.meta.json``.
The archive PDF is never modified; OCR output goes to ``ocr/<eid>.pdf`` (``process.ocr``).
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from kallimachos.interfaces import (
    SCHEMA_REGISTRY,
    KallimachosError,
    TextMeta,
    UsageError,
    build,
    dump,
    json_schema,
)
from kallimachos.paths import DataLayout
from kallimachos.state import store

PAGE_SEPARATOR = "\f"
OCR_MIN_CHARS_PER_PAGE = 200
OCR_MIN_ALPHA_RATIO = 0.5
OCR_MAX_SPACED_RATIO = 0.5
SPACED_MIN_TOKENS = 200

TEXT_META_SCHEMA = json_schema(SCHEMA_REGISTRY["text_meta.schema.json"][0])

Runner = Callable[[Sequence[str]], str]
"""Runs a command line and returns its stdout as text; injected by tests."""


class ToolMissing(KallimachosError):
    """An external tool (``pdftotext``, ``ocrmypdf``) is not installed (exit 1)."""


def eid_for(pdf: Path) -> str:
    """``sha256(pdf)[:12]``."""
    h = hashlib.sha256()
    with pdf.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def require_tool(tool: str, *, hint: str) -> str:
    """Return the resolved executable or raise :class:`ToolMissing` with a doctor hint."""
    found = shutil.which(tool)
    if found is None:
        raise ToolMissing(
            f"{tool!r} is not installed or not on PATH ({hint}); run `kallimachos doctor`"
        )
    return found


def _run(cmd: Sequence[str]) -> str:
    proc = subprocess.run(list(cmd), capture_output=True, check=False)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", errors="replace")[-200:].strip()
        raise KallimachosError(f"{cmd[0]} failed with exit {proc.returncode}: {tail}")
    return proc.stdout.decode("utf-8", errors="replace")


def pdf_to_pages(pdf: Path, *, tool: str = "pdftotext", runner: Runner | None = None) -> list[str]:
    """Run ``pdftotext -layout`` and split the output on form feeds (one string per page)."""
    if runner is None:
        exe = require_tool(tool, hint="install poppler")
        runner = _run
    else:
        exe = tool
    text = runner([exe, "-layout", "-enc", "UTF-8", str(pdf), "-"])
    return split_pages(text)


def split_pages(text: str) -> list[str]:
    pages = text.split(PAGE_SEPARATOR)
    if pages and pages[-1].strip() == "":
        pages.pop()  # pdftotext ends every page, including the last, with a form feed
    return pages


def text_stats(pages: Sequence[str]) -> tuple[int, float, float, int]:
    """Return ``(chars, alpha_ratio, spaced_ratio, tokens)``.

    ``chars`` counts non-whitespace characters, ``alpha_ratio`` the alphabetic share of them,
    ``spaced_ratio`` the share of single-letter alphabetic tokens (a letter-spaced OCR layer).
    """
    chars = 0
    alpha = 0
    tokens = 0
    single = 0
    for page in pages:
        for ch in page:
            if ch.isspace():
                continue
            chars += 1
            if ch.isalpha():
                alpha += 1
        for tok in page.split():
            tokens += 1
            if len(tok) == 1 and tok.isalpha():
                single += 1
    alpha_ratio = alpha / chars if chars else 0.0
    spaced_ratio = single / tokens if tokens else 0.0
    return chars, alpha_ratio, spaced_ratio, tokens


def needs_ocr(pages: list[str]) -> bool:
    """< 200 chars/page, alpha ratio < 0.5 or spaced-letter ratio > 0.5 (over 200 tokens)."""
    if not pages:
        return True
    chars, alpha_ratio, spaced_ratio, tokens = text_stats(pages)
    if chars / len(pages) < OCR_MIN_CHARS_PER_PAGE:
        return True
    if alpha_ratio < OCR_MIN_ALPHA_RATIO:
        return True
    return tokens > SPACED_MIN_TOKENS and spaced_ratio > OCR_MAX_SPACED_RATIO


def build_meta(eid: str, pdf: Path, pages: Sequence[str], *, note: Sequence[str] = ()) -> TextMeta:
    chars, alpha_ratio, spaced_ratio, _tokens = text_stats(pages)
    return TextMeta(
        eid=eid,
        path=str(pdf),
        pages=len(pages),
        chars=chars,
        ocr="needed" if needs_ocr(list(pages)) else "none",
        alpha_ratio=round(alpha_ratio, 4),
        spaced_ratio=round(spaced_ratio, 4),
        note=list(note),
    )


def text_path(layout: DataLayout, eid: str) -> Path:
    return layout.text_dir / f"{eid}.txt"


def meta_path(layout: DataLayout, eid: str) -> Path:
    return layout.text_dir / f"{eid}.meta.json"


def write_pages(layout: DataLayout, meta: TextMeta, pages: Sequence[str]) -> None:
    """Write ``text/<eid>.txt`` and ``text/<eid>.meta.json`` through ``state.store``."""
    store.write_text_atomic(text_path(layout, meta.eid), PAGE_SEPARATOR.join(pages))
    store.write_json_atomic(meta_path(layout, meta.eid), dump(meta), schema=TEXT_META_SCHEMA)


def extract_text(
    pdf: Path,
    layout: DataLayout,
    *,
    tool: str = "pdftotext",
    eid: str | None = None,
    runner: Runner | None = None,
) -> TextMeta:
    """Extract the text of ``pdf`` into the data directory and return its :class:`TextMeta`.

    ``eid`` defaults to ``eid_for(pdf)``; the caller passes the catalog eid when the PDF is an OCR
    copy under ``ocr/`` (the eid stays that of the archive PDF).
    """
    if not pdf.is_file():
        raise UsageError(f"PDF not found: {pdf}")
    eid = eid or eid_for(pdf)
    pages = pdf_to_pages(pdf, tool=tool, runner=runner)
    meta = build_meta(eid, pdf, pages)
    write_pages(layout, meta, pages)
    return meta


def load_meta(layout: DataLayout, eid: str) -> TextMeta | None:
    data = store.read_json(meta_path(layout, resolve_alias(layout, eid)))
    if data is None:
        return None
    return build(TextMeta, data, path="text_meta")


def resolve_alias(layout: DataLayout, eid: str) -> str:
    """Follow ``alias.json`` (``{old: new}``) to the current eid; cycles are cut."""
    aliases = store.read_json(layout.alias_file, default={})
    if not isinstance(aliases, dict):
        raise UsageError("alias.json must be a JSON object {old_eid: new_eid}")
    seen = {eid}
    while eid in aliases:
        nxt = aliases[eid]
        if not isinstance(nxt, str) or nxt in seen:
            break
        seen.add(nxt)
        eid = nxt
    return eid


def load_pages(layout: DataLayout, eid: str) -> list[str]:
    """Read ``text/<eid>.txt`` split on ``\f``; alias.json redirects old eids."""
    current = resolve_alias(layout, eid)
    path = text_path(layout, current)
    if not path.is_file():
        raise UsageError(f"no extracted text for {eid}; run `kallimachos process extract {eid}`")
    return path.read_text(encoding="utf-8").split(PAGE_SEPARATOR)
