"""Automatic related-notes block and fake-link repair (``process links``)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from kallimachos.config.schema import Config
from kallimachos.i18n import load_catalog
from kallimachos.interfaces import ApprovalRequired, GraphEdge, WorkState, build
from kallimachos.paths import DataLayout
from kallimachos.process import chunk as chunk_mod
from kallimachos.process import note as note_mod
from kallimachos.process import status as status_mod
from kallimachos.process.notes_index import NotesIndex, iter_notes
from kallimachos.state import store
from kallimachos.state.machine import at_least

RELATED_BLOCK_KIND = "related_notes"
MATH_CHARS = ",;+\u00d7\u2212=\\^_$\u2032'\u00b7()"
UNCERTAIN_REPORT = "links_uncertain.md"

_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+?)\]\]")
_ANY_WIKI_RE = re.compile(r"\[\[(.+?)\]\]")
_MD_LINK_RE = re.compile(r"\[([^\[\]\n]+)\]\(([^()\n]*)\)")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_LINK_TARGET_OK_RE = re.compile(
    r"^(?:https?://|ftp://|mailto:|obsidian:|file:|zotero:|#|\.{1,2}/|/)|\.[A-Za-z0-9]{1,5}(?:#.*)?$"
)


@dataclass
class LinkReport:
    updated: list[Path] = field(default_factory=list)
    skipped_existing: int = 0
    uncertain_report: Path | None = None
    fixes: list[tuple[Path, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Related-notes block
# ---------------------------------------------------------------------------


def _link_targets(text: str) -> set[str]:
    """Normalised targets of every ``[[...]]`` and ``[x](y)`` link in ``text``: full path without
    extension, and its basename."""
    out: set[str] = set()
    for m in _WIKILINK_RE.finditer(text):
        target = m.group(1).split("|", 1)[0].split("#", 1)[0].strip()
        out.update(_target_forms(target))
    for m in _MD_LINK_RE.finditer(text):
        out.update(_target_forms(m.group(2).split("#", 1)[0].strip()))
    return out


def _target_forms(target: str) -> set[str]:
    if not target:
        return set()
    p = PurePosixPath(target.replace("\\", "/"))
    stem = p.name[:-3] if p.name.endswith(".md") else p.name
    full = p.as_posix()[:-3] if p.as_posix().endswith(".md") else p.as_posix()
    return {stem, full, full.lstrip("./")}


def related_targets(
    eid: str, edges: list[GraphEdge], index: NotesIndex, note_text: str
) -> list[tuple[str, list[str]]]:
    """Target notes not already wikilinked outside the automatic block -> (path, reference keys).
    Edges to works without a note are skipped; ``path`` is the index path (root-relative)."""
    outside = note_mod.remove_managed_block(note_text, RELATED_BLOCK_KIND)
    linked = _link_targets(outside)
    grouped: dict[str, list[str]] = {}
    for edge in edges:
        if edge.source_eid != eid or edge.target_eid == eid:
            continue
        ref = index.get(edge.target_eid)
        if ref is None:
            continue
        forms = _target_forms(ref.path)
        if forms & linked:
            continue
        keys = grouped.setdefault(ref.path, [])
        if edge.key and edge.key not in keys:
            keys.append(edge.key)
    return sorted((path, keys) for path, keys in grouped.items())


def load_edges(path: Path) -> list[GraphEdge]:
    return [build(GraphEdge, raw) for raw in store.read_jsonl(path)]


def link_style(config: Config) -> str:
    if config.notes.link_style:
        return config.notes.link_style
    return "wikilink" if config.notes.target == "avenox" else "markdown"


def render_link(target_rel: str, *, title: str, from_rel: str, style: str) -> str:
    """``[[path-without-ext]]`` or ``[title](relative/path.md)`` from the linking note."""
    if style == "wikilink":
        target = target_rel[:-3] if target_rel.endswith(".md") else target_rel
        return f"[[{target}]]"
    src_dir = PurePosixPath(from_rel).parent
    # relative path from the note's directory
    src_parts = list(src_dir.parts)
    tgt_parts = list(PurePosixPath(target_rel).parts)
    common = 0
    while (
        common < len(src_parts)
        and common < len(tgt_parts) - 1
        and src_parts[common] == tgt_parts[common]
    ):
        common += 1
    rel = "../" * (len(src_parts) - common) + "/".join(tgt_parts[common:])
    return f"[{title}]({rel})"


def related_block(
    targets: list[tuple[str, list[str]]], *, index: NotesIndex, from_rel: str, style: str, lang: str
) -> str:
    catalog = load_catalog(lang)
    lines = [f"## {catalog['note.related']}"]
    for path, keys in targets:
        eid = index.by_path(path)
        ref = index.get(eid) if eid else None
        title = ref.title if ref else PurePosixPath(path).stem
        link = render_link(path, title=title, from_rel=from_rel, style=style)
        refs = ", ".join(f"[{k}]" for k in keys)
        lines.append(f"- {link} \u2014 references {refs}" if refs else f"- {link}")
    return "\n".join(lines)


def active_chunk_violations(layout: DataLayout) -> list[str]:
    """eids of active chunks (not every eid >= linked) that are still below ``note_written``."""
    states = status_mod.current_states(layout)
    if not layout.chunks_dir.is_dir():
        return []
    violations: list[str] = []
    for folder in sorted(layout.chunks_dir.iterdir()):
        if not folder.is_dir() or not folder.name.isdigit():
            continue
        chunk = chunk_mod.load_chunk(layout, int(folder.name))
        eids = list(chunk.eids)
        if not eids:
            continue
        current = {e: (states[e].state if e in states else None) for e in eids}
        if all(s is not None and at_least(s, WorkState.LINKED) for s in current.values()):
            continue  # closed chunk
        for e, s in current.items():
            if s is None or not at_least(s, WorkState.NOTE_WRITTEN):
                violations.append(e)
    return violations


def apply_links(layout: DataLayout, config: Config, *, apply: bool, force: bool) -> LinkReport:
    """Gate: an active chunk with eids below ``note_written``
    -> exit 5 unless ``force``."""
    violations = active_chunk_violations(layout)
    if violations and not force:
        raise ApprovalRequired(
            f"{len(violations)} eid(s) of an active chunk are below note_written "
            f"({', '.join(violations[:5])}); finish the reading wave or pass --force"
        )
    root = note_mod.vault_root(config)
    lang = config.notes.language or config.language
    style = link_style(config)
    edges = load_edges(layout.graph_file)
    index = NotesIndex.load(layout.notes_index_file)
    report = LinkReport()
    states = status_mod.current_states(layout)
    for eid, ref in index.items():
        path = root / ref.path
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        targets = related_targets(eid, edges, index, text)
        if targets:
            block = related_block(targets, index=index, from_rel=ref.path, style=style, lang=lang)
            new_text = note_mod.upsert_managed_block(
                text, kind=RELATED_BLOCK_KIND, block=block, attrs={}
            )
        else:
            new_text = note_mod.remove_managed_block(text, RELATED_BLOCK_KIND).rstrip("\n") + "\n"
            if new_text.strip() == text.strip():
                new_text = text
        if new_text != text:
            report.updated.append(path)
            if apply:
                store.write_text_atomic(path, new_text)
        else:
            report.skipped_existing += 1
        if apply:
            current = states.get(eid)
            if current is not None and current.state in (WorkState.NOTE_WRITTEN, WorkState.LINKED):
                status_mod.record_state(layout, eid, WorkState.LINKED, by="process links")
    uncertain = load_edges(layout.graph_uncertain_file)
    if uncertain:
        report.uncertain_report = layout.process / UNCERTAIN_REPORT
        if apply:
            store.write_text_atomic(
                report.uncertain_report, uncertain_report_text(uncertain, index)
            )
    return report


def uncertain_report_text(edges: list[GraphEdge], index: NotesIndex) -> str:
    lines = [
        "# Uncertain citation edges (not written into notes)",
        "",
        "Decide by hand; a confirmed edge belongs in graph.jsonl through `process cite`.",
        "",
    ]
    for e in edges:
        src = index.get(e.source_eid)
        tgt = index.get(e.target_eid)
        src_s = src.path if src else e.source_eid
        tgt_s = tgt.path if tgt else e.target_eid
        lines.append(f"- {src_s} -> {tgt_s} ({e.method}, {e.score:.1f}) [{e.key}]: {e.text}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Fake links
# ---------------------------------------------------------------------------


def _is_fake_wiki_target(inner: str, known_targets: set[str] | None) -> bool:
    target = inner.split("|", 1)[0].split("#", 1)[0].strip()
    if known_targets is not None and (_target_forms(target) & known_targets):
        return False
    return "[" in inner or "]" in inner or any(ch in MATH_CHARS for ch in inner)


def _is_fake_md_target(target: str) -> bool:
    target = target.strip()
    if not target:
        return True
    if _LINK_TARGET_OK_RE.search(target):
        return False
    if any(ch.isspace() for ch in target) or any(ch in MATH_CHARS for ch in target):
        return True
    return "/" not in target


def _fix_line(line: str, known_targets: set[str] | None, fixes: list[str]) -> str:
    # protect inline code
    spans: list[str] = []

    def _hold(m: re.Match[str]) -> str:
        spans.append(m.group(0))
        return f"\x00{len(spans) - 1}\x00"

    work = _INLINE_CODE_RE.sub(_hold, line)

    def _wiki(m: re.Match[str]) -> str:
        inner = m.group(1)
        if _is_fake_wiki_target(inner, known_targets):
            fixes.append(f"[[{inner}]] -> [ [{inner}] ]")
            return f"[ [{inner}] ]"
        return m.group(0)

    # lazy ``.+?`` reaches the first ``]]``, so ``[[a,0],[0,b]]`` is one (fake) link
    work = _ANY_WIKI_RE.sub(_wiki, work)

    def _md(m: re.Match[str]) -> str:
        text, target = m.group(1), m.group(2)
        if _is_fake_md_target(target):
            fixes.append(f"[{text}]({target}) -> [{text}] ({target})")
            return f"[{text}] ({target})"
        return m.group(0)

    work = _MD_LINK_RE.sub(_md, work)
    # unclosed [[ on this line
    while True:
        open_idx = work.rfind("[[")
        if open_idx == -1:
            break
        close_idx = work.find("]]", open_idx + 2)
        if close_idx != -1:
            break
        fixes.append(f"unclosed '[[' at column {open_idx + 1} -> '[ ['")
        work = work[:open_idx] + "[ [" + work[open_idx + 2 :]
    for i, span in enumerate(spans):
        work = work.replace(f"\x00{i}\x00", span)
    return work


def fix_fake_links(text: str, *, known_targets: set[str] | None = None) -> tuple[str, list[str]]:
    """Rewrite ``[[...]]`` without a target that contain math characters or nested brackets as
    ``[ [ ... ] ]``; fake ``[x](y)`` as ``[x] (y)`` (URLs, mailto, obsidian:, file extensions, ``#``
    excluded); unclosed ``[[`` on a line as ``[ [``; code blocks untouched.

    ``known_targets`` (stems and root-relative paths of existing notes) protects real wikilinks
    whose text happens to contain a math character.
    """
    fixes: list[str] = []
    out: list[str] = []
    in_code = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_code = not in_code
            out.append(line)
            continue
        if in_code:
            out.append(line)
            continue
        out.append(_fix_line(line, known_targets, fixes))
    return "\n".join(out), fixes


def known_targets_in(root: Path) -> set[str]:
    """Stems and root-relative paths (with and without ``.md``) of every file under ``root``."""
    out: set[str] = set()
    if not root.is_dir():
        return out
    for p in root.rglob("*"):
        if not p.is_file() or any(part.startswith(".") for part in p.relative_to(root).parts):
            continue
        rel = p.relative_to(root).as_posix()
        out.add(p.stem)
        out.add(p.name)
        out.add(rel)
        if rel.endswith(".md"):
            out.add(rel[:-3])
    return out


def fix_fake_in_folder(folder: Path, *, apply: bool, vault_root: Path | None = None) -> LinkReport:
    """Repair fake links in every note under ``folder``; existing targets are looked up in
    ``vault_root`` (default: ``folder``)."""
    known = known_targets_in(vault_root or folder)
    report = LinkReport()
    for path in iter_notes(folder):
        text = path.read_text(encoding="utf-8")
        fixed, fixes = fix_fake_links(text, known_targets=known)
        if not fixes:
            continue
        report.updated.append(path)
        report.fixes.extend((path, f) for f in fixes)
        if apply:
            store.write_text_atomic(path, fixed)
    return report


__all__ = [
    "LinkReport",
    "apply_links",
    "fix_fake_in_folder",
    "fix_fake_links",
    "related_targets",
]
