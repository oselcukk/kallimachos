"""PDF folders: first two pages via ``pdftotext`` (fallback ``pypdf``), DOI/arXiv regex; sensitive
paths are never opened.

Extractor order: ``pdftotext -l 2`` when installed, ``pypdf`` when importable, else a built-in
reader for simple (optionally Flate-compressed) content streams, which is enough for the
synthetic PDFs of the test-suite. Only the first two pages are read; nothing is written.

The sensitive-path check belongs to :func:`kallimachos.profile.from_files.collect`; ``parse`` on
a folder also accepts ``skip`` so that stand-alone callers can keep the same guarantee.
"""

from __future__ import annotations

import contextlib
import importlib
import re
import shutil
import subprocess
import zlib
from collections.abc import Callable
from pathlib import Path

from kallimachos.interfaces import WorkKind
from kallimachos.profile.importers import ImportedRef, find_arxiv, find_doi
from kallimachos.records.normalize import collapse_whitespace

Extractor = Callable[[Path], str]
_YEAR = re.compile(r"\b(1[6-9]\d\d|20\d\d)\b")
_SKIP_LINE = re.compile(
    r"(?i)^(arxiv|doi|https?://|www\.|abstract|keywords?|received|accepted|published|"
    r"copyright|©|preprint|submitted|vol\.|volume|journal|proceedings|issn|isbn)"
)
_NAME = r"[A-Z][\w.'\-]*(?:\s+[A-Z][\w.'\-]*){0,3}"
_NAME_LINE = re.compile(rf"^{_NAME}(?:\s*(?:,|and|&)\s*{_NAME})*$")
_PDF_STRING = re.compile(rb"\((?:\\.|[^\\)])*\)")
_PDF_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)


def parse(
    path: Path,
    *,
    tool: str | None = None,
    skip: Callable[[Path], bool] | None = None,
) -> list[ImportedRef]:
    """Parse one file or folder; never raises on malformed entries, skips them.

    ``tool`` forces an extractor (``pdftotext`` | ``pypdf`` | ``builtin``); ``None`` picks the
    first available. ``skip(path)`` returning True leaves that file unopened.
    """
    if path.is_dir():
        files = sorted(p for p in path.rglob("*.pdf") if not (skip and skip(p)))
    else:
        files = [] if (skip and skip(path)) else [path]
    extractor = select_extractor(tool)
    out: list[ImportedRef] = []
    for file in files:
        try:
            ref = ref_from_text(extractor(file), str(file))
        except Exception:  # a broken PDF must never stop the import
            ref = None
        if ref is not None:
            out.append(ref)
    return out


def select_extractor(tool: str | None = None) -> Extractor:
    """Pick the text extractor (see module docstring)."""
    if tool == "pdftotext" or (tool is None and pdftotext_available()):
        return _pdftotext
    if tool == "pypdf" or (tool is None and _pypdf_available()):
        return _pypdf
    return builtin_text


def pdftotext_available() -> bool:
    return shutil.which("pdftotext") is not None


def _pypdf_available() -> bool:
    try:
        importlib.import_module("pypdf")
    except ImportError:
        return False
    return True


def _pdftotext(path: Path) -> str:
    result = subprocess.run(  # fixed argv, no shell
        ["pdftotext", "-q", "-l", "2", "-layout", str(path), "-"],
        capture_output=True,
        check=False,
        timeout=60,
    )
    return result.stdout.decode("utf-8", errors="replace")


def _pypdf(path: Path) -> str:
    pypdf = importlib.import_module("pypdf")
    reader = pypdf.PdfReader(str(path))
    pages = list(reader.pages)[:2]
    return "\n".join(str(page.extract_text() or "") for page in pages)


def builtin_text(path: Path) -> str:
    """Text of the first two page content streams without external tools (simple PDFs only)."""
    data = path.read_bytes()
    chunks: list[str] = []
    pages_seen = 0
    for m in _PDF_STREAM.finditer(data):
        raw = m.group(1)
        with contextlib.suppress(zlib.error):
            raw = zlib.decompress(raw)
        if b"BT" not in raw:
            continue
        pages_seen += 1
        if pages_seen > 2:
            break
        lines: list[str] = []
        for line in raw.splitlines():
            if b"Tj" not in line and b"TJ" not in line:
                continue
            parts = [_unescape(s[1:-1]) for s in _PDF_STRING.findall(line)]
            if parts:
                lines.append("".join(parts))
        chunks.append("\n".join(lines))
    return "\n".join(chunks)


def _unescape(raw: bytes) -> str:
    text = raw.decode("latin-1")
    text = re.sub(r"\\([()\\])", r"\1", text)
    text = text.replace("\\n", "\n").replace("\\r", "").replace("\\t", " ")
    return text


def ref_from_text(text: str, source_file: str) -> ImportedRef | None:
    """Title/authors/year heuristics plus identifier regexes on first-pages text."""
    doi = find_doi(text)
    arxiv = find_arxiv(text)
    lines = [collapse_whitespace(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    title = ""
    title_idx = -1
    for idx, line in enumerate(lines[:40]):
        if _SKIP_LINE.match(line) or len(line) < 10 or len(line) > 200:
            continue
        words = line.split()
        if len(words) < 3 or line[0].isdigit():
            continue
        title, title_idx = line, idx
        break
    if not title:
        if not (doi or arxiv):
            return None
        title = Path(source_file).stem.replace("_", " ").replace("-", " ")
    authors: list[str] = []
    if title_idx >= 0:
        for line in lines[title_idx + 1 : title_idx + 4]:
            if _SKIP_LINE.match(line) or any(ch.isdigit() for ch in line):
                continue
            if _NAME_LINE.match(line) and len(line.split()) <= 12:
                authors = [
                    collapse_whitespace(a)
                    for a in re.split(r"\s*(?:,|\band\b|&)\s*", line)
                    if a.strip()
                ]
                break
    year: int | None = None
    head = "\n".join(lines[:60])
    years = [int(y) for y in _YEAR.findall(head)]
    if years:
        year = max(years)
    kind = WorkKind.PREPRINT if arxiv else (WorkKind.ARTICLE if doi else WorkKind.UNKNOWN)
    return ImportedRef(
        title=title,
        source_file=source_file,
        year=year,
        authors=authors,
        doi=doi,
        arxiv=arxiv,
        kind=kind,
    )
