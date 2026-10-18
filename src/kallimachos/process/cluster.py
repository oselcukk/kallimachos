"""Cluster summary and open-problem table (``process cluster``).

``cluster start FOLDER`` gates on every eid of the folder being ``>= linked`` and returns the
skeleton plus the input packet (note paths, graph edges, card items); the agent writes the summary
from notes, list file and citation graph only; ``cluster write FOLDER --file`` validates and writes
``<FOLDER>/_Cluster-Summary.md``. ``open-problems`` prints the closure table skeleton.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kallimachos.config.schema import Config
from kallimachos.i18n import load_catalog
from kallimachos.interfaces import (
    ApprovalRequired,
    Card,
    ClusterFrontmatter,
    GraphEdge,
    ItemStatus,
    ItemType,
    UsageError,
    WorkState,
    dump,
)
from kallimachos.paths import DataLayout
from kallimachos.process import avenox
from kallimachos.process import card as card_mod
from kallimachos.process import links as links_mod
from kallimachos.process import note as note_mod
from kallimachos.process import status as status_mod
from kallimachos.process.notes_index import eid_of, iter_notes, note_title, parse_frontmatter
from kallimachos.records.normalize import match_key
from kallimachos.state import store
from kallimachos.state.machine import at_least

SECTION_KEYS: tuple[str, ...] = (
    "cluster.section.map",
    "cluster.section.glossary",
    "cluster.section.numerical",
    "cluster.section.errors",
    "cluster.section.suggested",
)
COMMENT_TAG = "[comment]"
VERIFIED_MARK = "<!-- kallimachos:verified_by {model} -->"
OPEN_TYPES: frozenset[ItemType] = frozenset(
    {ItemType.OPEN_PROBLEM, ItemType.QUESTION, ItemType.CONJECTURE, ItemType.RESEARCH_DIRECTION}
)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_NUMBER_RE = re.compile(r"^\d+(\.\d+)*\.?\s*")
_LINK_RE = re.compile(r"\[\[[^\]]+\]\]|\]\(")


@dataclass
class ClusterPacket:
    folder: Path
    note_paths: list[Path] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    card_items: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    skeleton_md: str = ""
    eids: dict[str, Path] = field(default_factory=dict)


def resolve_folder(folder: Path, config: Config) -> Path:
    """Absolute cluster folder: as given, else relative to the literature folder, else the vault."""
    if folder.is_absolute():
        return folder
    lit = note_mod.literature_root(config)
    if (lit / folder).is_dir():
        return lit / folder
    return note_mod.vault_root(config) / folder


def folder_notes(folder: Path) -> dict[str, Path]:
    """eid -> note path for every note of the folder (recursive, ``_`` files skipped)."""
    out: dict[str, Path] = {}
    for path in iter_notes(folder):
        try:
            eid = eid_of(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if eid:
            out[eid] = path
    return out


def cluster_name(folder: Path, config: Config) -> str:
    try:
        return folder.resolve().relative_to(note_mod.literature_root(config).resolve()).as_posix()
    except (ValueError, UsageError):
        return folder.name


def cluster_start(folder: Path, layout: DataLayout, config: Config) -> ClusterPacket:
    """Gate: every eid of the folder must be >= ``linked`` else exit 5."""
    folder = resolve_folder(folder, config)
    if not folder.is_dir():
        raise UsageError(f"cluster folder not found: {folder}")
    notes = folder_notes(folder)
    if not notes:
        raise UsageError(f"no literature notes with an eid under {folder}")
    states = status_mod.current_states(layout)
    behind = [
        eid
        for eid in notes
        if eid not in states or not at_least(states[eid].state, WorkState.LINKED)
    ]
    if behind:
        raise ApprovalRequired(
            f"{len(behind)} work(s) of {folder.name} are below 'linked' "
            f"({', '.join(sorted(behind)[:5])}); run 'process links --apply' first"
        )
    edges = [
        e
        for e in links_mod.load_edges(layout.graph_file)
        if e.source_eid in notes and e.target_eid in notes
    ]
    items: dict[str, list[dict[str, object]]] = {}
    for eid in sorted(notes):
        card = card_mod.load_card(layout, eid)
        items[eid] = [dict(dump(item)) for item in card.items] if card else []
    lang = config.notes.language or config.language
    skeleton = skeleton_md(folder, config, notes, lang=lang)
    return ClusterPacket(
        folder=folder,
        note_paths=[notes[e] for e in sorted(notes)],
        edges=edges,
        card_items=items,
        skeleton_md=skeleton,
        eids=dict(sorted(notes.items())),
    )


def section_headings(lang: str) -> list[str]:
    catalog = load_catalog(lang)
    return [catalog[k] for k in SECTION_KEYS]


def skeleton_md(folder: Path, config: Config, notes: dict[str, Path], *, lang: str) -> str:
    heads = section_headings(lang)
    root = note_mod.vault_root(config)
    lines = [
        f"# Cluster summary: {cluster_name(folder, config)}",
        "",
        "<!-- Intro paragraph (method): built only from the notes below, the list file and the "
        "citation graph; PDFs are not re-read; every bullet links to a note; anything not in "
        f"the sources carries the {COMMENT_TAG} tag. Note the citation graph's limits. -->",
        "",
    ]
    for i, head in enumerate(heads, 1):
        lines.append(f"## {i}. {head}")
        if i == 1:
            lines.extend(
                [f"### {i}.1 Main lines", "", f"### {i}.2 Chronology and influence chain", ""]
            )
        else:
            lines.append("")
    lines.extend(["<!-- Notes in this cluster: -->"])
    for eid in sorted(notes):
        try:
            rel = notes[eid].resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = notes[eid].as_posix()
        lines.append(f"<!-- {eid} {rel} -->")
    return "\n".join(lines) + "\n"


def _sections(body: str) -> list[tuple[int, str, int]]:
    out: list[tuple[int, str, int]] = []
    in_code = False
    for no, line in enumerate(body.splitlines(), 1):
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = _HEADING_RE.match(line)
        if m:
            out.append((len(m.group(1)), _NUMBER_RE.sub("", m.group(2)), no))
    return out


def validate_cluster_body(body: str, *, lang: str) -> list[str]:
    """Sections, method paragraph, ``[comment]`` tag and link rule; returns warnings, raises
    ``UsageError`` on hard failures."""
    if body.lstrip().startswith("---"):
        raise UsageError("cluster body must not carry frontmatter; cluster write generates it")
    heads = _sections(body)
    h1 = [no for lvl, _t, no in heads if lvl == 1]
    if not h1:
        raise UsageError("cluster summary needs one H1")
    present = {match_key(t) for lvl, t, _n in heads if lvl == 2}
    missing = [h for h in section_headings(lang) if match_key(h) not in present]
    if missing:
        raise UsageError("missing cluster sections: " + ", ".join(repr(m) for m in missing))
    lines = body.splitlines()
    first_h2 = min((no for lvl, _t, no in heads if lvl == 2), default=len(lines) + 1)
    intro = [
        ln.strip()
        for ln in lines[h1[0] : first_h2 - 1]
        if ln.strip() and not ln.strip().startswith(("#", "<!--"))
    ]
    if not intro:
        raise UsageError(
            "method paragraph missing: state before the first section that the summary rests "
            "on notes, list file and citation graph only and that unsourced text is tagged "
            f"{COMMENT_TAG}"
        )
    if COMMENT_TAG not in " ".join(intro):
        raise UsageError(f"method paragraph must declare the {COMMENT_TAG} tag convention")
    _fixed, fixes = links_mod.fix_fake_links(body)
    if fixes:
        raise UsageError(f"fake links in the cluster summary: {'; '.join(fixes[:5])}")
    warnings: list[str] = []
    unlinked = [
        no
        for no, ln in enumerate(lines, 1)
        if ln.lstrip().startswith(("- ", "* ", "+ "))
        and not _LINK_RE.search(ln)
        and COMMENT_TAG not in ln
    ]
    if unlinked:
        raise UsageError(
            f"{len(unlinked)} bullet(s) link to no note and carry no {COMMENT_TAG} tag "
            f"(lines {', '.join(str(n) for n in unlinked[:8])})"
        )
    return warnings


def cluster_frontmatter(folder: Path, config: Config, *, work_count: int) -> ClusterFrontmatter:
    fm_cfg = config.notes.frontmatter
    return ClusterFrontmatter(
        cluster=cluster_name(folder, config),
        project=fm_cfg.project or "",
        work_count=work_count,
        updated_at=_dt.datetime.now(tz=_dt.UTC).date(),
        kind=fm_cfg.kind,
        visibility=fm_cfg.visibility,
    )


def render_cluster_frontmatter(fm: ClusterFrontmatter) -> str:
    data = dump(fm)
    order = ("kind", "project", "visibility", "updated_at", "cluster", "work_count", "kallimachos")
    lines = ["---", *(f"{k}: {note_mod.yaml_scalar(data[k])}" for k in order if k in data), "---"]
    return "\n".join(lines) + "\n"


def cluster_write(
    folder: Path, body: str, layout: DataLayout, config: Config, *, verified_by: str | None
) -> Path:
    """Validate sections, the method paragraph and ``[comment]`` tags; write
    ``_Cluster-Summary.md``. An existing summary is replaced only when it is Kallimachos's own
    (``kallimachos: cluster/1`` in its frontmatter); any other file at that path -> exit 5."""
    folder = resolve_folder(folder, config)
    notes = folder_notes(folder)
    if not notes:
        raise UsageError(f"no literature notes with an eid under {folder}")
    lang = config.notes.language or config.language
    validate_cluster_body(body, lang=lang)
    target = folder / config.notes.cluster_summary_name
    if target.exists():
        fm, _b = parse_frontmatter(target.read_text(encoding="utf-8"))
        if fm.get("kallimachos") != "cluster/1":
            raise ApprovalRequired(
                f"{target} exists and is not a Kallimachos cluster summary; move it or rename "
                "notes.cluster_summary_name"
            )
    fm_obj = cluster_frontmatter(folder, config, work_count=len(notes))
    text = render_cluster_frontmatter(fm_obj)
    if verified_by:
        text += VERIFIED_MARK.format(model=verified_by.replace("-->", "")) + "\n"
    text += "\n" + body.strip("\n") + "\n"
    store.write_text_atomic(target, text)
    states = status_mod.current_states(layout)
    for eid in sorted(notes):
        current = states.get(eid)
        if current is not None and current.state in (
            WorkState.LINKED,
            WorkState.CLUSTER_SUMMARIZED,
        ):
            status_mod.record_state(
                layout, eid, WorkState.CLUSTER_SUMMARIZED, by="process cluster write"
            )
    if config.notes.target == "avenox":
        info = avenox.detect(note_mod.vault_root(config))
        if info is not None:
            avenox.sync(info)
    return target


# ---------------------------------------------------------------------------
# Open problems
# ---------------------------------------------------------------------------


def _item_key(label: str, number: str | None) -> str:
    return match_key(f"{label} {number or ''}")


def solved_elsewhere(cards: dict[str, Card], edges: list[GraphEdge]) -> set[tuple[str, str]]:
    """``(eid, item key)`` pairs that another work in the archive claims to solve: a
    ``claimed_solution`` item whose ``target`` names the problem, in a card that cites the work."""
    cited_by: dict[str, set[str]] = {}
    for e in edges:
        cited_by.setdefault(e.target_eid, set()).add(e.source_eid)
    out: set[tuple[str, str]] = set()
    for target_eid, card in cards.items():
        for item in card.items:
            if item.status is ItemStatus.SOLVED_ELSEWHERE:
                out.add((target_eid, _item_key(item.label, item.number)))
        for solver_eid in cited_by.get(target_eid, set()):
            solver = cards.get(solver_eid)
            if solver is None:
                continue
            for item in solver.items:
                if item.type is ItemType.CLAIMED_SOLUTION and item.target:
                    tkey = match_key(item.target)
                    for problem in card.items:
                        pkey = _item_key(problem.label, problem.number)
                        if tkey and (tkey in pkey or pkey in tkey):
                            out.add((target_eid, pkey))
    return out


def open_problems_table(folder: Path, layout: DataLayout, config: Config) -> str:
    """Skeleton ``# | Work | Problem (author's words, p.) | Concreteness | Status | Evidence``;
    problems solved elsewhere (card status + graph) are not proposed as open."""
    folder = resolve_folder(folder, config)
    notes = folder_notes(folder)
    if not notes:
        raise UsageError(f"no literature notes with an eid under {folder}")
    cards: dict[str, Card] = {}
    for eid in notes:
        card = card_mod.load_card(layout, eid)
        if card is not None:
            cards[eid] = card
    edges = links_mod.load_edges(layout.graph_file)
    closed = solved_elsewhere(cards, edges)
    root = note_mod.vault_root(config)
    style = links_mod.link_style(config)
    rows: list[str] = []
    for eid in sorted(cards):
        card = cards[eid]
        try:
            rel = notes[eid].resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = notes[eid].as_posix()
        title = note_title(notes[eid].read_text(encoding="utf-8"), notes[eid].stem)
        link = links_mod.render_link(rel, title=title, from_rel=rel, style=style)
        for item in card.items:
            is_open_item = item.type in OPEN_TYPES or item.status in (
                ItemStatus.OPEN,
                ItemStatus.PARTLY_SOLVED_HERE,
            )
            if not is_open_item or item.status is ItemStatus.SOLVED_HERE:
                continue
            if (eid, _item_key(item.label, item.number)) in closed:
                continue
            quote = item.quote.replace("|", "\\|").replace("\n", " ")
            page = item.printed_page or str(item.page)
            concreteness = item.concreteness or ""
            status = "partly" if item.status is ItemStatus.PARTLY_SOLVED_HERE else "open"
            problem = f'"{quote}" (p. {page})'
            rows.append(f"| {len(rows) + 1} | {link} | {problem} | {concreteness} | {status} | |")
    header = [
        f"# Open problems: {cluster_name(folder, config)}",
        "",
        "Status is filled by the agent from the notes and the citation graph only; a problem "
        "solved in another work of the archive is not listed as open. Comments carry "
        f"{COMMENT_TAG}.",
        "",
        "| # | Work | Problem (author's words, p.) | Concreteness | Status | Evidence |",
        "|---|---|---|---|---|---|",
    ]
    return "\n".join([*header, *rows]) + "\n"


def packet_json(packet: ClusterPacket) -> dict[str, Any]:
    return {
        "folder": str(packet.folder),
        "note_paths": [str(p) for p in packet.note_paths],
        "eids": {eid: str(p) for eid, p in packet.eids.items()},
        "edges": [dump(e) for e in packet.edges],
        "card_items": packet.card_items,
        "skeleton_md": packet.skeleton_md,
    }
