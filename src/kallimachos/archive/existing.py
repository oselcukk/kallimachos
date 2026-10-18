"""Read-only scan of an existing PDF library.

Never moves or renames; skips macOS dataless files; incremental cache on (path, size, mtime);
privacy gate before any Crossref/arXiv query; output ``existing_catalog.jsonl`` + ``duplicates.md``.

Read-only means: files under ``ROOT`` are only ``stat``-ed and read (sha256, first two pages via
``pdftotext``, ``pdfinfo`` title). Nothing under ``ROOT`` is created, renamed, moved or deleted.
Paths matching ``archive.sensitive_path_regex`` (checked on the path relative to ``ROOT``) are
neither opened nor recorded. Only strong identifiers (DOI, arXiv id) are ever sent to an API, and
only for entries that pass :func:`privacy_gate`.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from kallimachos.archive.files import is_dataless
from kallimachos.archive.files import sha256_file as sha256_of
from kallimachos.archive.identity import find_dois, normalize_doi, split_arxiv_version
from kallimachos.archive.rows import ExistingEntry, read_rows, write_rows
from kallimachos.config.schema import Config
from kallimachos.data import load_json
from kallimachos.interfaces import (
    HttpClient,
    Ident,
    IdentKind,
    Record,
    Source,
    WorkKind,
    build,
    dump,
)
from kallimachos.paths import DataLayout
from kallimachos.state import store

EXTRACTOR_VERSION = 1
CACHE_FILE = "existing_cache.json"
DUPLICATES_MD = "duplicates.md"
QUERYABLE_KINDS = frozenset(
    {WorkKind.ARTICLE, WorkKind.PREPRINT, WorkKind.BOOK, WorkKind.THESIS, WorkKind.CHAPTER}
)
COURSE_MATERIAL_RE = re.compile(
    r"(?i)(lecture|course|syllabus|slides?|tutorial|assignment|solutions?|grades?|transcript|"
    r"\bcv\b|resume|invoice|receipt|contract)"
)
_REF_HEAD_RE = re.compile(r"(?im)^\s*(references|bibliography|literature cited)\s*$")
_ARXIV_TEXT_RE = re.compile(
    r"arXiv:\s*(?P<id>[0-9]{4}\.[0-9]{4,5}|[a-z-]+(?:\.[A-Z]{2})?/[0-9]{7})(?P<v>v[0-9]+)?"
)
_THESIS_RE = re.compile(
    r"(?i)\b(dissertation|ph\.?\s?d\.?\s+thesis|doctoral thesis|master'?s thesis)\b"
)


@dataclass
class ExistingScanReport:
    scanned: int = 0
    identified: int = 0
    skipped_sensitive: int = 0
    skipped_dataless: int = 0
    duplicates: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    queried: int = 0
    from_cache: int = 0


@dataclass(frozen=True)
class FilenamePattern:
    id: str
    regex: re.Pattern[str]
    kind: str
    doi_template: str | None


@lru_cache(maxsize=1)
def filename_patterns() -> tuple[FilenamePattern, ...]:
    raw = load_json("publisher_filename_patterns.json")
    return tuple(
        FilenamePattern(p["id"], re.compile(p["regex"]), p["kind"], p.get("doi_template"))
        for p in raw["patterns"]
    )


def is_sensitive(rel_path: str, patterns: Sequence[str]) -> bool:
    return any(re.search(p, rel_path) for p in patterns)


def iter_pdfs(root: Path, *, exclude: Sequence[str], skip: Sequence[Path]) -> Iterator[Path]:
    """PDF files below ``root``; symlinks are not followed; excluded globs (relative path or
    directory name) and ``skip`` directories are pruned."""
    skip_resolved = {s.resolve() for s in skip}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        kept = []
        for d in sorted(dirnames):
            sub = here / d
            rel = sub.relative_to(root).as_posix()
            if sub.is_symlink() or sub.resolve() in skip_resolved or d.startswith("."):
                continue
            if any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(d, g) for g in exclude):
                continue
            kept.append(d)
        dirnames[:] = kept
        for name in sorted(filenames):
            path = here / name
            if name.lower().endswith(".pdf") and not path.is_symlink() and path.is_file():
                rel = path.relative_to(root).as_posix()
                if not any(fnmatch.fnmatch(rel, g) for g in exclude):
                    yield path


def filename_idents(name: str) -> dict[str, str]:
    """Identifiers recoverable from a file name (publisher patterns, arXiv, DOI)."""
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    out: dict[str, str] = {}
    for pat in filename_patterns():
        m = pat.regex.search(stem if pat.kind != "doi_underscore" else name)
        if not m:
            continue
        if pat.kind == "arxiv" and "arxiv" not in out:
            out["arxiv"] = m.group("id")
            if m.groupdict().get("version"):
                out["arxiv_version"] = m.group("version")
        elif pat.kind == "arxiv_old" and "arxiv" not in out:
            archive = m.group("archive")
            if archive.split(".")[0] in _OLD_ARCHIVES:
                out["arxiv"] = f"{archive}/{m.group('num')}"
                if m.group("version"):
                    out["arxiv_version"] = m.group("version")
        elif pat.kind == "doi_underscore" and "doi" not in out:
            out["doi"] = normalize_doi(m.group("id").replace("_", "/", 1))
        elif pat.kind == "doi" and pat.doi_template and "doi" not in out:
            out["doi"] = normalize_doi(pat.doi_template.format(id=m.group("id")))
        elif pat.kind == "pii" and "pii" not in out:
            out["pii"] = m.group("id")
    return out


_OLD_ARCHIVES = frozenset(
    [
        "math",
        "hep-th",
        "hep-ph",
        "hep-ex",
        "hep-lat",
        "gr-qc",
        "quant-ph",
        "math-ph",
        "nucl-th",
        "nucl-ex",
        "astro-ph",
        "cond-mat",
        "cs",
        "nlin",
        "physics",
        "q-bio",
        "q-fin",
        "stat",
        "alg-geom",
        "dg-ga",
        "funct-an",
        "q-alg",
    ]
)


def text_idents(text: str) -> dict[str, str]:
    """DOI / arXiv id from the first pages; everything after a references heading is ignored."""
    m = _REF_HEAD_RE.search(text)
    head = text[: m.start()] if m else text
    out: dict[str, str] = {}
    dois = find_dois(head)
    if dois:
        out["doi"] = dois[0]
    am = _ARXIV_TEXT_RE.search(head)
    if am:
        out["arxiv"] = am.group("id")
        if am.group("v"):
            out["arxiv_version"] = am.group("v")
    return out


def _run(args: list[str]) -> str:
    tool = shutil.which(args[0])
    if tool is None:
        return ""
    try:
        res = subprocess.run(
            [tool, *args[1:]], capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout if res.returncode == 0 else ""


def first_pages_text(path: Path) -> str:
    return _run(["pdftotext", "-f", "1", "-l", "2", "-enc", "UTF-8", str(path), "-"])


def pdfinfo_fields(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in _run(["pdfinfo", str(path)]).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


def guess_kind(entry: ExistingEntry, text: str, pages: int | None) -> WorkKind:
    if _THESIS_RE.search(text[:4000]):
        return WorkKind.THESIS
    if entry.doi:
        return WorkKind.ARTICLE if not (pages and pages > 150) else WorkKind.BOOK
    if entry.arxiv or entry.eprint:
        return WorkKind.PREPRINT
    return WorkKind.UNKNOWN


def privacy_gate(entry: ExistingEntry, sensitive: Sequence[str]) -> tuple[bool, str]:
    """Privacy gate: only strong-id literature records may be sent out. Default is refusal."""
    if is_sensitive(entry.path, sensitive):
        return False, "sensitive path"
    if COURSE_MATERIAL_RE.search(entry.path):
        return False, "course or personal material path"
    if not (entry.doi or entry.arxiv):
        return False, "no strong identifier"
    if entry.kind_guess not in QUERYABLE_KINDS:
        return False, f"kind {entry.kind_guess.value} is not literature"
    return True, ""


def _lookup(entry: ExistingEntry, sources: Mapping[str, Source], http: HttpClient) -> Record | None:
    attempts: list[tuple[str, Ident]] = []
    if entry.doi:
        attempts += [
            ("crossref", Ident(IdentKind.DOI, entry.doi)),
            ("openalex", Ident(IdentKind.DOI, entry.doi)),
        ]
    if entry.arxiv:
        attempts.append(("arxiv", Ident(IdentKind.ARXIV, entry.arxiv)))
    for name, ident in attempts:
        source = sources.get(name)
        if source is None:
            continue
        try:
            rec = source.metadata(ident, http)
        except Exception:
            rec = None
        if rec is not None:
            return rec
    return None


def _keys(entry: ExistingEntry) -> list[str]:
    keys = []
    if entry.doi:
        keys.append(f"doi:{entry.doi}")
    if entry.arxiv:
        keys.append(f"arxiv:{entry.arxiv}")
    if entry.eprint:
        keys.append(f"eprint:{entry.eprint}")
    return keys


def _identify(path: Path, rel: str, root: Path, st: os.stat_result) -> ExistingEntry:
    entry = ExistingEntry(
        path=rel,
        root=str(root),
        size=st.st_size,
        mtime=st.st_mtime,
        extractor_version=EXTRACTOR_VERSION,
    )
    found = filename_idents(path.name)
    if found:
        entry.identified_by.append("filename")
    text = first_pages_text(path)
    from_text = text_idents(text)
    if from_text and not found.keys() >= from_text.keys():
        entry.identified_by.append("text")
    merged = {**from_text, **found}
    info = pdfinfo_fields(path)
    title = info.get("Title", "").strip()
    if title and len(title.split()) >= 3:
        entry.title = title
        entry.identified_by.append("pdfinfo")
    entry.doi = merged.get("doi")
    if merged.get("arxiv"):
        entry.arxiv, ver = split_arxiv_version(merged["arxiv"])
        entry.arxiv_version = merged.get("arxiv_version") or ver
    entry.pii = merged.get("pii")
    pages = int(info["Pages"]) if info.get("Pages", "").isdigit() else None
    entry.sha256 = sha256_of(path)
    entry.kind_guess = guess_kind(entry, text, pages)
    entry.keys = _keys(entry)
    return entry


def scan_existing(
    root: Path,
    layout: DataLayout,
    config: Config,
    http: HttpClient,
    *,
    dry_run: bool = False,
    sources: Mapping[str, Source] | None = None,
) -> ExistingScanReport:
    """Catalog ``root`` read-only. ``sources`` enables metadata lookups for entries that pass the
    privacy gate; without it no request is made at all."""
    report = ExistingScanReport()
    root = root.expanduser().resolve()
    if not root.is_dir():
        report.errors.append(f"not a directory: {root}")
        return report
    sensitive = config.archive.sensitive_path_regex
    skip = [Path(config.archive.root).expanduser()] if config.archive.root else []
    cache_path = layout.archive / CACHE_FILE
    cache: dict[str, Any] = store.read_json(cache_path, default={}) or {}
    new_cache: dict[str, Any] = {}
    rows: list[ExistingEntry] = []
    for path in iter_pdfs(root, exclude=config.archive.existing_exclude, skip=skip):
        rel = path.relative_to(root).as_posix()
        if is_sensitive(rel, sensitive):
            report.skipped_sensitive += 1  # never opened, never recorded
            continue
        report.scanned += 1
        try:
            st = os.stat(path)
        except OSError as exc:
            report.errors.append(f"{rel}: {exc.strerror}")
            continue
        cache_key = str(path)
        hit = cache.get(cache_key)
        if (
            isinstance(hit, dict)
            and hit.get("size") == st.st_size
            and hit.get("mtime") == st.st_mtime
            and hit.get("version") == EXTRACTOR_VERSION
        ):
            entry = build(ExistingEntry, hit["row"])
            report.from_cache += 1
        elif is_dataless(path):
            report.skipped_dataless += 1
            entry = ExistingEntry(
                path=rel, root=str(root), size=st.st_size, mtime=st.st_mtime, local=False
            )
        else:
            try:
                entry = _identify(path, rel, root, st)
            except OSError as exc:
                report.errors.append(f"{rel}: {exc.strerror}")
                continue
            ok, _why = privacy_gate(entry, sensitive)
            if ok and sources:
                rec = _lookup(entry, sources, http)
                entry.queried = True
                report.queried += 1
                if rec is not None:
                    _apply_lookup(entry, rec)
        if entry.local:
            new_cache[cache_key] = {
                "size": st.st_size,
                "mtime": st.st_mtime,
                "version": EXTRACTOR_VERSION,
                "row": dump(entry),
            }
        if entry.keys or entry.pii:
            report.identified += 1
        rows.append(entry)
    report.duplicates = find_duplicates(rows)
    if not dry_run:
        others = [
            r for r in read_rows(layout.existing_catalog_file, ExistingEntry) if r.root != str(root)
        ]
        write_rows(layout.existing_catalog_file, [*others, *rows])
        store.write_text_atomic(layout.archive / DUPLICATES_MD, duplicates_markdown(root, report))
        kept = {k: v for k, v in cache.items() if not k.startswith(str(root) + os.sep)}
        store.write_json_atomic(cache_path, {**kept, **new_cache})
    return report


def _apply_lookup(entry: ExistingEntry, rec: Record) -> None:
    if rec.title:
        entry.title = rec.title
    if rec.authors:
        entry.authors = [a.name for a in rec.authors][:30]
    date = rec.published or rec.created
    if date:
        entry.year = date.year
    entry.identified_by.append("lookup")


def find_duplicates(rows: Sequence[ExistingEntry]) -> list[tuple[str, str]]:
    """Pairs of paths with the same sha256 or the same identity key."""
    pairs: list[tuple[str, str]] = []
    first: dict[str, str] = {}
    for row in rows:
        marks = [f"sha:{row.sha256}"] if row.sha256 else []
        marks += row.keys
        reported: set[str] = set()
        for mark in marks:
            other = first.get(mark)
            if other is None:
                first[mark] = row.path
            elif other != row.path and other not in reported:
                pairs.append((other, row.path))
                reported.add(other)
    return pairs


def duplicates_markdown(root: Path, report: ExistingScanReport) -> str:
    lines = [
        "<!-- generated by kallimachos archive scan-existing; nothing was moved or deleted -->",
        "# Duplicates",
        "",
        f"Root: `{root}`",
        "",
    ]
    if not report.duplicates:
        lines.append("No duplicates found.")
    for a, b in report.duplicates:
        lines.append(f"- `{a}` = `{b}`")
    return "\n".join(lines) + "\n"
