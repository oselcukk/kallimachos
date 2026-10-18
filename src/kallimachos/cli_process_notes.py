"""``process`` note commands (notes, links, cluster, queue, notes-index).

note-write, links [fix-fake], cluster start|write|open-problems, queue add|sync, notes-index
rebuild|find. Registered under the shared `process` group created by cli.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from kallimachos.cli_support import add_command, add_group, eid_arg, register_leaf
from kallimachos.config import load as config_load
from kallimachos.config.schema import Config
from kallimachos.interfaces import (
    ApprovalRequired,
    ExitCode,
    PartialFailure,
    UsageError,
    build,
    dump,
)
from kallimachos.paths import DataLayout
from kallimachos.process import cluster as cluster_mod
from kallimachos.process import links as links_mod
from kallimachos.process import note as note_mod
from kallimachos.process import queue as queue_mod
from kallimachos.process.notes_index import NotesIndex
from kallimachos.profile.schema import Profile
from kallimachos.state import store


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = add_command(
        sub,
        "note-write",
        cmd_note_write,
        help="write the literature note from a body file",
        path="process note-write",
    )
    p.add_argument("eid", metavar="EID", type=eid_arg)
    p.add_argument(
        "--file", required=True, metavar="NOTE_MD", help="note body written by the reader"
    )
    p.add_argument("--existing", metavar="PATH", help="update only the managed block of this note")
    p.add_argument("--reader", metavar="MODEL", help="reader model name (default: config)")

    links = sub.add_parser(
        "links", help="automatic related-notes block; fix-fake repairs fake links"
    )
    register_leaf(links, cmd_links, path="process links")
    links.add_argument("--apply", action="store_true", help="write (default: dry run)")
    links.add_argument("--force", action="store_true", help="ignore the active-chunk gate")
    lsub = links.add_subparsers(dest="links_command", metavar="<command>")
    p = add_command(
        lsub,
        "fix-fake",
        cmd_links_fix_fake,
        help="repair fake [[ ]] and [x](y) links",
        path="process links fix-fake",
    )
    p.add_argument("--apply", action="store_true", help="write (default: dry run)")
    p.add_argument("--all", action="store_true", help="whole vault (requires approval)")
    c = add_group(
        sub, "cluster", help="cluster summary and open-problem list", path="process cluster"
    )
    p = add_command(
        c,
        "start",
        cmd_cluster_start,
        help="skeleton + input packet for a folder",
        path="process cluster start",
    )
    p.add_argument("folder", metavar="FOLDER", help="literature subfolder (relative to the vault)")
    p = add_command(
        c,
        "write",
        cmd_cluster_write,
        help="validate and write _Cluster-Summary.md",
        path="process cluster write",
    )
    p.add_argument("folder", metavar="FOLDER", help="literature subfolder (relative to the vault)")
    p.add_argument("--file", required=True, metavar="MD", help="summary body written by the model")
    p.add_argument("--verified-by", metavar="MODEL", help="verifier model recorded in frontmatter")
    p = add_command(
        c,
        "open-problems",
        cmd_cluster_open_problems,
        help="open-problem table skeleton",
        path="process cluster open-problems",
    )
    p.add_argument("folder", metavar="FOLDER", help="literature subfolder (relative to the vault)")

    q = add_group(sub, "queue", help="reading queue (read/ask)", path="process queue")
    p = add_command(q, "add", cmd_queue_add, help="add an item", path="process queue add")
    p.add_argument("ident", metavar="EID_OR_CANDIDATE_ID", help="eid or run candidate id")
    p.add_argument(
        "--kind", required=True, choices=["read", "ask"], help="read it, or ask the advisor"
    )
    p.add_argument("--note", metavar="TEXT", help="free note stored with the item")
    p.add_argument("--title", metavar="TEXT", help="title when the id is not in catalog/runs")
    p.add_argument("--url", metavar="URL", help="url when the id is not in catalog/runs")
    add_command(
        q,
        "sync",
        cmd_queue_sync,
        help="deliver to Avenox tasks or the queue file",
        path="process queue sync",
    )

    n = add_group(sub, "notes-index", help="eid -> note path index", path="process notes-index")
    add_command(
        n,
        "rebuild",
        cmd_notes_index_rebuild,
        help="rebuild from frontmatter",
        path="process notes-index rebuild",
    )
    p = add_command(
        n,
        "find",
        cmd_notes_index_find,
        help="find notes by surname and year",
        path="process notes-index find",
    )
    p.add_argument("surname", metavar="SURNAME", help="first author's surname")
    p.add_argument("year", nargs="?", type=int, metavar="YEAR", help="publication year")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _context(args: argparse.Namespace) -> tuple[Config, DataLayout]:
    config, layout = config_load.load_config(data_dir=getattr(args, "data_dir", None))
    return config, layout


def load_profile(layout: DataLayout) -> Profile:
    """``<data_dir>/profile.yaml`` as :class:`Profile`; a missing file is the empty profile."""
    path = layout.profile_file
    if not path.is_file():
        return Profile()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise UsageError(f"profile.yaml is not valid YAML: {exc}") from None
    if data is None:
        return Profile()
    if not isinstance(data, dict):
        raise UsageError("profile.yaml must be a mapping")
    return build(Profile, data)


def _emit(args: argparse.Namespace, data: dict[str, Any], human: list[str]) -> None:
    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        for line in human:
            print(line)


def _read_file(path_s: str) -> str:
    path = Path(path_s)
    if not path.is_file():
        raise UsageError(f"file not found: {path}")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def cmd_note_write(args: argparse.Namespace) -> int:
    """process.note.write_note; exit 5 without a semantic pass or on name clash without --existing;
    exit 3 on Avenox sync conflict."""
    config, layout = _context(args)
    profile = load_profile(layout)
    body = _read_file(args.file)
    existing = Path(args.existing) if args.existing else None
    result = note_mod.write_note(
        layout,
        config,
        profile,
        args.eid,
        body,
        existing=existing,
        reader=args.reader,
        dry_run=bool(getattr(args, "dry_run", False)),
    )
    data = {
        "path": str(result.path),
        "created": result.created,
        "frontmatter": dump(result.frontmatter),
        "warnings": result.warnings,
        "sync": result.sync_result,
        "dry_run": bool(getattr(args, "dry_run", False)),
    }
    verb = "would write" if data["dry_run"] else ("wrote" if result.created else "updated")
    human = [f"{verb} {result.path}"]
    human.extend(f"warning: {w}" for w in result.warnings)
    if result.sync_result:
        human.append(f"avenox sync: {result.sync_result}")
    _emit(args, data, human)
    if result.sync_result not in (None, "succeeded", "degraded"):
        raise PartialFailure(
            f"note written but Avenox sync returned {result.sync_result}; "
            "run 'beyin.py sync' in the vault to inspect"
        )
    return int(ExitCode.OK)


def cmd_links(args: argparse.Namespace) -> int:
    """process.links.apply_links (dry run unless --apply)."""
    config, layout = _context(args)
    apply = bool(args.apply) and not getattr(args, "dry_run", False)
    report = links_mod.apply_links(layout, config, apply=apply, force=bool(args.force))
    data = {
        "apply": apply,
        "updated": [str(p) for p in report.updated],
        "unchanged": report.skipped_existing,
        "uncertain_report": str(report.uncertain_report) if report.uncertain_report else None,
    }
    human = [f"{'updated' if apply else 'would update'} {len(report.updated)} note(s)"]
    human.extend(f"  {p}" for p in report.updated)
    if report.uncertain_report:
        human.append(f"uncertain edges report: {report.uncertain_report}")
    _emit(args, data, human)
    return int(ExitCode.OK)


def cmd_links_fix_fake(args: argparse.Namespace) -> int:
    """process.links.fix_fake_in_folder."""
    config, _layout = _context(args)
    root = note_mod.vault_root(config)
    if args.all:
        if not getattr(args, "yes", False):
            raise ApprovalRequired(
                f"--all touches every Markdown file under {root}; list the folders to the user "
                "and rerun with --yes after approval"
            )
        folder = root
    else:
        folder = note_mod.literature_root(config)
    apply = bool(args.apply) and not getattr(args, "dry_run", False)
    report = links_mod.fix_fake_in_folder(folder, apply=apply, vault_root=root)
    data = {
        "apply": apply,
        "folder": str(folder),
        "files": [str(p) for p in report.updated],
        "fixes": [{"file": str(p), "fix": f} for p, f in report.fixes],
    }
    verb = "fixed" if apply else "would fix"
    human = [f"{verb} {len(report.fixes)} link(s) in {len(report.updated)} file(s)"]
    human.extend(f"  {p}: {f}" for p, f in report.fixes)
    _emit(args, data, human)
    return int(ExitCode.OK)


def cmd_cluster_start(args: argparse.Namespace) -> int:
    """process.cluster.cluster_start; exit 5 when an eid is below linked."""
    config, layout = _context(args)
    packet = cluster_mod.cluster_start(Path(args.folder), layout, config)
    data = cluster_mod.packet_json(packet)
    human = [
        f"cluster folder: {packet.folder}",
        f"notes: {len(packet.note_paths)} - edges: {len(packet.edges)}",
        "",
        packet.skeleton_md,
    ]
    _emit(args, data, human)
    return int(ExitCode.OK)


def cmd_cluster_write(args: argparse.Namespace) -> int:
    """process.cluster.cluster_write."""
    config, layout = _context(args)
    body = _read_file(args.file)
    if getattr(args, "dry_run", False):
        lang = config.notes.language or config.language
        cluster_mod.validate_cluster_body(body, lang=lang)
        _emit(args, {"valid": True, "dry_run": True}, ["cluster summary body is valid"])
        return int(ExitCode.OK)
    path = cluster_mod.cluster_write(
        Path(args.folder), body, layout, config, verified_by=args.verified_by
    )
    _emit(args, {"path": str(path)}, [f"wrote {path}"])
    return int(ExitCode.OK)


def cmd_cluster_open_problems(args: argparse.Namespace) -> int:
    """process.cluster.open_problems_table."""
    config, layout = _context(args)
    table = cluster_mod.open_problems_table(Path(args.folder), layout, config)
    _emit(args, {"table_md": table}, [table])
    return int(ExitCode.OK)


def _lookup_work(layout: DataLayout, ident: str) -> tuple[str, str | None]:
    """(title, url) for an eid or candidate id from the catalog, else from run files."""
    for raw in store.read_jsonl(layout.catalog_file):
        keys = raw.get("keys") or []
        if raw.get("id") == ident or ident in keys:
            url = raw.get("oa_url") or (f"https://doi.org/{raw['doi']}" if raw.get("doi") else None)
            return str(raw.get("title", "")), url
    if layout.runs.is_dir():
        for run_file in sorted(layout.runs.glob("*.json"), reverse=True):
            if run_file.name.endswith(".selection.json"):
                continue
            data = store.read_json(run_file, default={})
            for cand in data.get("candidates", []) if isinstance(data, dict) else []:
                if isinstance(cand, dict) and cand.get("id") == ident:
                    return str(cand.get("title", "")), cand.get("url")
    return "", None


def cmd_queue_add(args: argparse.Namespace) -> int:
    """process.queue.queue_add."""
    _config, layout = _context(args)
    title, url = _lookup_work(layout, args.ident)
    title = args.title or title
    url = args.url or url
    if not title:
        raise UsageError(
            f"{args.ident} is neither in the catalog nor in a run file; pass --title (and --url)"
        )
    if getattr(args, "dry_run", False):
        _emit(
            args,
            {"ident": args.ident, "kind": args.kind, "title": title, "url": url},
            [f"would queue {args.kind}: {title}"],
        )
        return int(ExitCode.OK)
    entry = queue_mod.queue_add(
        layout, ident=args.ident, kind=args.kind, title=title, url=url, note=args.note
    )
    _emit(args, dump(entry), [f"queued {entry.kind}: {entry.title}"])
    return int(ExitCode.OK)


def cmd_queue_sync(args: argparse.Namespace) -> int:
    """process.queue.queue_sync."""
    config, layout = _context(args)
    if getattr(args, "dry_run", False):
        pending = [e for e in queue_mod.current_entries(layout).values() if e.synced is None]
        _emit(args, {"pending": [dump(e) for e in pending]}, [f"{len(pending)} entry(ies) pending"])
        return int(ExitCode.OK)
    synced = queue_mod.queue_sync(layout, config)
    human = [f"synced {len(synced)} entry(ies)"]
    for e in synced:
        target = (
            f"task {e.synced.avenox_task_id}"
            if e.synced and e.synced.avenox_task_id
            else f"queue file line {e.synced.queue_file_line if e.synced else '?'}"
        )
        human.append(f"  {e.kind}: {e.title} -> {target}")
    _emit(args, {"synced": [dump(e) for e in synced]}, human)
    return int(ExitCode.OK)


def cmd_notes_index_rebuild(args: argparse.Namespace) -> int:
    """NotesIndex.rebuild."""
    config, layout = _context(args)
    root = note_mod.vault_root(config)
    index = NotesIndex(layout.notes_index_file)
    count = index.rebuild(note_mod.literature_root(config), root)
    if not getattr(args, "dry_run", False):
        index.save()
    _emit(
        args, {"count": count, "path": str(layout.notes_index_file)}, [f"indexed {count} note(s)"]
    )
    return int(ExitCode.OK)


def cmd_notes_index_find(args: argparse.Namespace) -> int:
    """NotesIndex.find."""
    _config, layout = _context(args)
    index = NotesIndex.load(layout.notes_index_file)
    hits = index.find(args.surname, args.year)
    data = {"hits": [{"eid": eid, **dump(ref)} for eid, ref in hits]}
    human = [f"{eid}  {ref.path}  {ref.title}" for eid, ref in hits] or ["no note found"]
    _emit(args, data, human)
    if not hits:
        print("hint: a wikilink is only allowed to a work that has a note", file=sys.stderr)
    return int(ExitCode.OK)
