"""``process`` core commands: text extraction, quotes, cards, checks, status, Zotero.

chunk, extract, quote, card-write, check, cite, semantic-record, status, migrate-card, zotero.
Registered under the shared ``process`` group created by cli.py. Every state transition these
commands make goes through ``process.status.record_state``; agents never edit
``status.jsonl`` by hand.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from kallimachos import secrets
from kallimachos.archive.catalog import Catalog
from kallimachos.cli_support import add_command, add_group, eid_arg
from kallimachos.config import load as config_load
from kallimachos.config.schema import Config
from kallimachos.interfaces import (
    SCHEMA_REGISTRY,
    ExitCode,
    GraphEdge,
    HttpClient,
    SchemaViolation,
    UsageError,
    WorkState,
    dump,
    json_schema,
)
from kallimachos.paths import DataLayout
from kallimachos.process import card as card_mod
from kallimachos.process import check as check_mod
from kallimachos.process import chunk as chunk_mod
from kallimachos.process import cite as cite_mod
from kallimachos.process import extract as extract_mod
from kallimachos.process import ocr as ocr_mod
from kallimachos.process import quote as quote_mod
from kallimachos.process import semantic as semantic_mod
from kallimachos.process import status as status_mod
from kallimachos.process.zotero import ZoteroClient, item_data
from kallimachos.process.zotero import citekey as citekey_mod
from kallimachos.process.zotero import link as link_mod
from kallimachos.process.zotero import metadata as metadata_mod
from kallimachos.process.zotero import names as names_mod
from kallimachos.process.zotero import ris as ris_mod
from kallimachos.profile import load as profile_load
from kallimachos.state import store

GRAPH_EDGE_SCHEMA = json_schema(SCHEMA_REGISTRY["graph_edge.schema.json"][0])


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    g = add_group(sub, "chunk", help="reading batches", path="process chunk")
    p = add_command(
        g,
        "create",
        cmd_chunk_create,
        help="create a batch for a channel",
        path="process chunk create",
    )
    p.add_argument("--channel", required=True, metavar="C", help="profile channel id (A, B, ...)")
    p.add_argument(
        "--size", type=int, metavar="N", help="works per batch (default: process.chunk.size)"
    )

    p = add_command(
        sub, "extract", cmd_extract, help="PDF -> text (+ OCR decision)", path="process extract"
    )
    p.add_argument("eids", nargs="*", metavar="EID", help="eids or PDF paths; default: catalog")
    p.add_argument("--ocr", action="store_true", help="run OCR when the PDF has no text layer")
    p.add_argument("--redo", action="store_true", help="re-extract existing text")

    p = add_command(
        sub, "quote", cmd_quote, help="verbatim quote from the text", path="process quote"
    )
    p.add_argument("eid", metavar="EID", type=eid_arg)
    p.add_argument("page", type=int, metavar="PAGE", help="1-based page number")
    p.add_argument("start", metavar="START", help="first words of the quote, as printed")
    p.add_argument("--end", metavar="END", help="last words of the quote (or use --length)")
    p.add_argument("--length", type=int, metavar="N", help="quote length in characters")

    p = add_command(
        sub,
        "card-write",
        cmd_card_write,
        help="validate and store a card (card/4 JSON)",
        path="process card-write",
    )
    p.add_argument("eid", metavar="EID", type=eid_arg)
    p.add_argument("--file", required=True, metavar="JSON", help="card JSON written by the reader")

    p = add_command(
        sub, "check", cmd_check, help="verify a card against the text", path="process check"
    )
    p.add_argument("eid", metavar="EID", type=eid_arg)
    p.add_argument("--pre", action="store_true", help="report only, write nothing")

    add_command(sub, "cite", cmd_cite, help="build the citation graph", path="process cite")

    p = add_command(
        sub,
        "semantic-record",
        cmd_semantic_record,
        help="record the verifier result",
        path="process semantic-record",
    )
    p.add_argument("eid", metavar="EID", type=eid_arg)
    p.add_argument("--file", required=True, metavar="JSON", help="verifier result JSON")

    p = add_command(
        sub, "status", cmd_status, help="state table from status.jsonl", path="process status"
    )
    p.add_argument("--summary", action="store_true", help="print the summary text for the receipt")

    p = add_command(
        sub,
        "migrate-card",
        cmd_migrate_card,
        help="convert an old card format",
        path="process migrate-card",
    )
    p.add_argument(
        "--from", dest="source_format", required=True, choices=["tr-v4"], help="old card format"
    )
    p.add_argument("--file", required=True, metavar="JSON", help="old card to convert")
    p.add_argument("--out", metavar="JSON", help="write here instead of stdout")

    z = add_group(sub, "zotero", help="Zotero export and maintenance", path="process zotero")
    p = add_command(
        z, "ris", cmd_zotero_ris, help="RIS files from catalog + cards", path="process zotero ris"
    )
    p.add_argument("--only-new", action="store_true", help="skip works already exported")
    p.add_argument("--out-dir", metavar="DIR", help="default: <data>/process/ris")
    p = add_command(
        z,
        "fix-metadata",
        cmd_zotero_fix_metadata,
        help="fix item metadata (JSONL report)",
        path="process zotero fix-metadata",
    )
    p.add_argument("--redo", action="store_true", help="revisit items fixed earlier")
    add_command(
        z,
        "split-names",
        cmd_zotero_split_names,
        help="split creators with empty first names",
        path="process zotero split-names",
    )
    p = add_command(
        z,
        "reset-citekeys",
        cmd_zotero_reset_citekeys,
        help="clear citation keys",
        path="process zotero reset-citekeys",
    )
    p.add_argument("--keys", metavar="KEYS", help="comma-separated Zotero item keys (default: all)")
    p.add_argument("--filter", metavar="EXPR", help="only items whose title or key contains EXPR")
    p = add_command(
        z,
        "link",
        cmd_zotero_link,
        help="link Zotero items and notes via eid",
        path="process zotero link",
    )
    p.add_argument("--vault-name", metavar="NAME", help="Obsidian vault name for obsidian:// links")


# --- shared helpers ------------------------------------------------------------------------------


def _context(args: argparse.Namespace) -> tuple[Config, DataLayout]:
    config, layout = config_load.load_config(data_dir=getattr(args, "data_dir", None))
    return config, layout


def _flag(args: argparse.Namespace, name: str) -> bool:
    return bool(getattr(args, name, False))


def _emit(args: argparse.Namespace, payload: Any, text: str | Iterable[str]) -> None:
    if _flag(args, "json"):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(text if isinstance(text, str) else "\n".join(text))


def _read_json_file(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise UsageError(f"file not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UsageError(f"{p}: invalid JSON ({exc})") from None
    if not isinstance(data, dict):
        raise UsageError(f"{p}: expected a JSON object")
    return data


def _catalog(layout: DataLayout) -> Catalog:
    if not layout.catalog_file.is_file():
        return Catalog(layout.catalog_file)
    return Catalog.load(layout.catalog_file)


def _http(config: Config, layout: DataLayout) -> HttpClient:
    """The shared client without the response cache: Zotero and metadata lookups must see the
    library as it is now."""
    from kallimachos.http.client import from_config

    return from_config(config, layout, with_cache=False)


def _zotero(config: Config, layout: DataLayout, http: HttpClient | None = None) -> ZoteroClient:
    zcfg = config.process.zotero
    if not zcfg.enabled:
        raise UsageError("process.zotero.enabled is false")
    api_key = secrets.resolve_ref(zcfg.api_key_ref) if zcfg.api_key_ref else None
    return ZoteroClient(zcfg, http or _http(config, layout), api_key=api_key)


def _entry_for(catalog: Catalog, eid: str) -> Any:
    for entry in catalog.entries():
        if entry.sha256 and entry.sha256[:12] == eid:
            return entry
    return None


def _pdf_for(config: Config, catalog: Catalog, eid: str) -> Path:
    entry = _entry_for(catalog, eid)
    if entry is None or not entry.path:
        raise UsageError(f"{eid}: not in the catalog; pass the PDF path instead")
    root = Path(config.archive.root).expanduser() if config.archive.root else Path()
    return root / str(entry.path)


def _write_jsonl(path: Path, edges: Sequence[GraphEdge]) -> None:
    store.write_jsonl_atomic(path, edges, schema=GRAPH_EDGE_SCHEMA)


# --- handlers -------------------------------------------------------------------------------------


def cmd_chunk_create(args: argparse.Namespace) -> int:
    """process.chunk.create_chunk; writes chunks/<n>/{list,groups}.json and log.md."""
    config, layout = _context(args)
    size = args.size or config.process.chunk.size
    if _flag(args, "dry_run"):
        print("dry run: chunk not created", file=sys.stderr)
        return int(ExitCode.OK)
    chunk = chunk_mod.create_chunk(
        _catalog(layout), layout, config.process, channel=args.channel, size=size
    )
    payload = {
        "chunk": chunk.number,
        "channel": chunk.channel,
        "works": len(chunk.eids),
        "groups": chunk.groups,
        "books": chunk.books,
        "dir": str(chunk_mod.chunk_dir(layout, chunk.number)),
    }
    _emit(
        args,
        payload,
        [
            f"chunk {chunk.number}: {len(chunk.eids)} work(s) in {len(chunk.groups)} group(s), "
            f"{len(chunk.books)} book(s) set aside",
            f"files: {payload['dir']}",
        ],
    )
    return int(ExitCode.OK)


def _resolve_targets(
    config: Config, catalog: Catalog, layout: DataLayout, raw: Sequence[str]
) -> list[tuple[str, Path]]:
    """``(eid, pdf)`` for every argument: an eid looked up in the catalog or a PDF path."""
    out: list[tuple[str, Path]] = []
    if not raw:
        root = Path(config.archive.root).expanduser() if config.archive.root else Path()
        for entry in catalog.entries():
            if entry.sha256 and entry.path:
                out.append((entry.sha256[:12], root / entry.path))
        return out
    for item in raw:
        path = Path(item).expanduser()
        if path.is_file():
            out.append((extract_mod.eid_for(path), path))
        else:
            out.append((item, _pdf_for(config, catalog, item)))
    return out


def cmd_extract(args: argparse.Namespace) -> int:
    """process.extract + OCR decision; records text_extracted / ocr_done."""
    config, layout = _context(args)
    catalog = _catalog(layout)
    targets = _resolve_targets(config, catalog, layout, args.eids)
    dry = _flag(args, "dry_run")
    by = "process extract"
    results: list[dict[str, Any]] = []
    failures = 0
    for eid, pdf in targets:
        if not args.redo and extract_mod.meta_path(layout, eid).is_file():
            meta = extract_mod.load_meta(layout, eid)
            if meta is not None and (meta.ocr != "needed" or not args.ocr):
                results.append({"eid": eid, "pages": meta.pages, "ocr": meta.ocr, "skipped": True})
                continue
        if dry:
            results.append({"eid": eid, "pdf": str(pdf), "dry_run": True})
            continue
        try:
            meta = extract_mod.extract_text(pdf, layout, tool=config.process.text_tool, eid=eid)
            status_mod.ensure_started(layout, eid, by=by)
            status_mod.record_state(layout, eid, WorkState.TEXT_EXTRACTED, by=by)
            if meta.ocr == "needed" and args.ocr:
                meta = _run_ocr(config, layout, eid, pdf)
                status_mod.record_state(layout, eid, WorkState.OCR_DONE, by=by)
        except extract_mod.ToolMissing:
            raise
        except (UsageError, SchemaViolation):
            raise
        except Exception as exc:  # one bad PDF must not stop the batch (exit 3)
            failures += 1
            results.append({"eid": eid, "pdf": str(pdf), "error": str(exc)[:200]})
            print(f"{eid}: {exc}", file=sys.stderr)
            continue
        row = dump(meta)
        results.append(row)
        if meta.ocr == "needed" and not args.ocr:
            hint = (
                "--ocr (process.ocr_tool: ocrmypdf)"
                if config.process.ocr_tool != "none"
                else "set process.ocr_tool: ocrmypdf and run with --ocr"
            )
            print(f"{eid}: OCR needed; rerun with {hint}", file=sys.stderr)
    _emit(
        args,
        results,
        [
            f"{r.get('eid')}: "
            + (
                f"error {r['error']}"
                if "error" in r
                else f"{r.get('pages', '?')} page(s), ocr={r.get('ocr', '-')}"
                + (" (skipped)" if r.get("skipped") else "")
            )
            for r in results
        ],
    )
    if failures and failures == len(targets):
        return int(ExitCode.UNEXPECTED)
    return int(ExitCode.PARTIAL if failures else ExitCode.OK)


def _run_ocr(config: Config, layout: DataLayout, eid: str, pdf: Path) -> Any:
    if config.process.ocr_tool == "none":
        raise UsageError("process.ocr_tool is 'none'; set it to ocrmypdf to use --ocr")
    out = layout.ocr_dir / f"{eid}.pdf"
    ocr_mod.ocr_pdf(pdf, out, langs=config.process.ocr_langs)
    pages = extract_mod.pdf_to_pages(out, tool=config.process.text_tool)
    note = ["ocr"]
    if extract_mod.needs_ocr(pages):
        ocr_mod.ocr_pdf(pdf, out, langs=config.process.ocr_langs, force=True)
        pages = extract_mod.pdf_to_pages(out, tool=config.process.text_tool)
        note.append("force-ocr")
    meta = extract_mod.build_meta(eid, pdf, pages, note=note)
    meta.ocr = "done"
    extract_mod.write_pages(layout, meta, pages)
    return meta


def cmd_quote(args: argparse.Namespace) -> int:
    """process.quote: exit 0 found, 1 not found verbatim, 2 usage."""
    _config, layout = _context(args)
    pages = extract_mod.load_pages(layout, args.eid)
    result = quote_mod.quote(pages, args.page, args.start, end=args.end, length=args.length)
    if result.exit_code == 2:
        print("usage: page out of range, empty START, or both --end and --length", file=sys.stderr)
        return int(ExitCode.USAGE)
    if result.exit_code == 1:
        print(f"not found on page {args.page}", file=sys.stderr)
        return int(ExitCode.UNEXPECTED)
    _emit(
        args,
        {
            "eid": args.eid,
            "page": result.page,
            "start": result.start_offset,
            "end": result.end_offset,
            "chars": len(result.text),
            "text": result.text,
        },
        result.text,
    )
    return int(ExitCode.OK)


def cmd_card_write(args: argparse.Namespace) -> int:
    """Validate card/4 and record card_written."""
    _config, layout = _context(args)
    data = _read_json_file(args.file)
    data.setdefault("eid", args.eid)
    card = card_mod.validate_card(data)
    if card.eid != args.eid:
        raise UsageError(f"card is for {card.eid}, command was given {args.eid}")
    if _flag(args, "dry_run"):
        _emit(args, {"eid": card.eid, "valid": True, "dry_run": True}, f"{card.eid}: card valid")
        return int(ExitCode.OK)
    path = card_mod.write_card(layout, card)
    chunk_mod.log_event(layout, [card.eid], f"card-write {card.eid}: {len(card.items)} item(s)")
    _emit(
        args,
        {"eid": card.eid, "path": str(path), "items": len(card.items), "state": "card_written"},
        f"{card.eid}: card written ({len(card.items)} item(s)) -> {path}",
    )
    return int(ExitCode.OK)


def cmd_check(args: argparse.Namespace) -> int:
    """process.check; records check_passed / check_failed unless --pre."""
    config, layout = _context(args)
    card = card_mod.load_card(layout, args.eid)
    if card is None:
        raise UsageError(f"no card for {args.eid}; run `process card-write` first")
    pages = extract_mod.load_pages(layout, args.eid)
    langs = [config.language] if config.language != "en" else []
    profile = profile_load.load_profile(layout.profile_file, missing_ok=True)
    report = check_mod.check_card(
        card,
        pages,
        config.process,
        pre=args.pre,
        langs=["en", *langs],
        extra_phrases=profile.open_problem_phrases,
    )
    write = not args.pre and not _flag(args, "dry_run")
    if write:
        store.write_json_atomic(layout.checks_dir / f"{args.eid}.json", report.to_dict())
        status_mod.record_state(layout, args.eid, WorkState(report.result), by="process check")
        chunk_mod.log_event(layout, [args.eid], f"check {args.eid}: {report.result}")
    lines = [f"{args.eid}: {report.result}" + (" (pre, nothing written)" if args.pre else "")]
    found = sum(1 for q in report.quotes if q.found)
    lines.append(f"quotes: {found}/{len(report.quotes)} found")
    lines += [
        f"  item {q.item_index}: {'found' if q.found else 'NOT FOUND'} p.{q.page}"
        + (f" -> page_fixed {q.page_fixed}" if q.page_fixed else "")
        + f" ({q.score:.0f})"
        for q in report.quotes
        if not q.found or q.page_fixed
    ]
    lines += [f"missing: {m}" for m in report.missing_items]
    lines += [f"warning: {w}" for w in report.scope_warnings]
    lines += [f"schema: {e}" for e in report.schema_errors]
    _emit(args, report.to_dict(), lines)
    return int(ExitCode.OK)


def cmd_cite(args: argparse.Namespace) -> int:
    """process.cite over all cards; writes graph.jsonl and graph_uncertain.jsonl."""
    _config, layout = _context(args)
    cards = card_mod.load_all_cards(layout)
    targets = cite_mod.build_targets(_catalog(layout).entries(), cards)
    certain: list[GraphEdge] = []
    uncertain: list[GraphEdge] = []
    for card in cards:
        c, u = cite_mod.match_against(card, targets)
        certain.extend(c)
        uncertain.extend(u)
    if not _flag(args, "dry_run"):
        _write_jsonl(layout.graph_file, certain)
        _write_jsonl(layout.graph_uncertain_file, uncertain)
    _emit(
        args,
        {
            "cards": len(cards),
            "certain": [dump(e) for e in certain],
            "uncertain": [dump(e) for e in uncertain],
        },
        f"{len(cards)} card(s): {len(certain)} certain edge(s), {len(uncertain)} uncertain",
    )
    return int(ExitCode.OK)


def cmd_semantic_record(args: argparse.Namespace) -> int:
    """Validate semantic_result/1 and record the state."""
    _config, layout = _context(args)
    result = semantic_mod.validate_semantic(_read_json_file(args.file), eid=args.eid)
    if _flag(args, "dry_run"):
        _emit(args, {"eid": args.eid, "result": result.result, "dry_run": True}, "valid")
        return int(ExitCode.OK)
    path = semantic_mod.record_semantic(layout, result)
    chunk_mod.log_event(
        layout, [args.eid], f"semantic-record {args.eid}: {result.result} ({result.verifier_model})"
    )
    _emit(
        args,
        {"eid": args.eid, "result": result.result, "path": str(path)},
        f"{args.eid}: semantic {result.result} ({len(result.mismatches)} mismatch(es)) -> {path}",
    )
    return int(ExitCode.OK)


def cmd_status(args: argparse.Namespace) -> int:
    """Table or --summary text."""
    config, layout = _context(args)
    if args.summary:
        text = status_mod.summary_text(layout, lang=config.language)
        _emit(args, {"summary": text}, text)
        return int(ExitCode.OK)
    rows = status_mod.status_table(layout)
    lines = ["eid           state               at                         by"]
    lines += [f"{r['eid']:<13} {r['state']:<19} {r['at']:<26} {r['by']}" for r in rows]
    _emit(args, rows, lines)
    return int(ExitCode.OK)


def cmd_migrate_card(args: argparse.Namespace) -> int:
    """process.card.migrate_card_tr_v4."""
    data = _read_json_file(args.file)
    if args.source_format != "tr-v4":
        raise UsageError(f"unknown source format {args.source_format!r}")
    migrated = card_mod.migrate_card_tr_v4(data)
    text = json.dumps(migrated, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        if not _flag(args, "dry_run"):
            store.write_text_atomic(Path(args.out), text)
        print(f"migrated card written to {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return int(ExitCode.OK)


def cmd_zotero_ris(args: argparse.Namespace) -> int:
    """process.zotero.ris."""
    config, layout = _context(args)
    cards = {c.eid: c for c in card_mod.load_all_cards(layout)}
    root = Path(config.archive.root).expanduser() if config.archive.root else None
    texts = ris_mod.catalog_to_ris(
        _catalog(layout).entries(), cards, only_new=args.only_new, root=root
    )
    out_dir = Path(args.out_dir) if args.out_dir else layout.process / "ris"
    written: list[str] = []
    for category, text in texts.items():
        path = out_dir / f"{category}.ris"
        if not _flag(args, "dry_run"):
            store.write_text_atomic(path, text)
        written.append(str(path))
    _emit(args, {"files": written}, [f"{len(texts)} RIS file(s)", *written])
    return int(ExitCode.OK)


def cmd_zotero_fix_metadata(args: argparse.Namespace) -> int:
    """process.zotero.metadata."""
    config, layout = _context(args)
    http = _http(config, layout)
    client = _zotero(config, layout, http)
    stamp = _dt.datetime.now(tz=_dt.UTC).strftime("%Y%m%d-%H%M%S")
    report = layout.root / "reports" / f"zotero-fix-{stamp}.jsonl"
    rows = metadata_mod.fix_metadata(
        client,
        http,
        report_path=report,
        cache_path=layout.cache / "zotero_meta.sqlite",
        dry_run=_flag(args, "dry_run"),
        redo=args.redo,
    )
    fixed = sum(1 for r in rows if r["status"] in ("fixed", "dry_run"))
    _emit(
        args,
        {"report": str(report), "items": len(rows), "fixed": fixed},
        f"{len(rows)} item(s), {fixed} fixed; report {report}",
    )
    return int(ExitCode.OK)


def cmd_zotero_split_names(args: argparse.Namespace) -> int:
    """process.zotero.names."""
    config, layout = _context(args)
    client = _zotero(config, layout)
    changed: list[dict[str, Any]] = []
    for item in client.items():
        data = dict(item_data(item))
        if data.get("itemType") in metadata_mod.SKIP_TYPES:
            continue
        if names_mod.split_creators(data):
            data.setdefault("key", item.get("key"))
            data.setdefault("version", item.get("version"))
            changed.append(data)
    client.update_items(changed, dry_run=_flag(args, "dry_run"))
    _emit(args, {"changed": len(changed)}, f"{len(changed)} item(s) with split creator names")
    return int(ExitCode.OK)


def cmd_zotero_reset_citekeys(args: argparse.Namespace) -> int:
    """process.zotero.citekey."""
    config, layout = _context(args)
    client = _zotero(config, layout)
    keys = [k for k in (args.keys or "").split(",") if k.strip()] or None
    count = citekey_mod.reset_citekeys(
        client, keys=keys, item_filter=args.filter, dry_run=_flag(args, "dry_run")
    )
    _emit(args, {"reset": count}, f"{count} citation key(s) cleared")
    return int(ExitCode.OK)


def cmd_zotero_link(args: argparse.Namespace) -> int:
    """process.zotero.link; chunk-close only."""
    config, layout = _context(args)
    client = _zotero(config, layout)
    notes = config.notes
    root = Path(notes.vault_path).expanduser() if notes.vault_path else None
    link_style = notes.link_style or ("wikilink" if notes.target == "avenox" else "markdown")
    vault_name = args.vault_name or (root.name if root and notes.target == "avenox" else None)
    report = link_mod.link_notes(
        client,
        layout,
        vault_name=vault_name,
        link_style=link_style,
        dry_run=_flag(args, "dry_run"),
        notes_root=root,
    )
    linked = sum(1 for r in report if r["status"] == "linked")
    _emit(
        args,
        report,
        [f"{len(report)} note(s), {linked} linked"]
        + [f"{r['eid']}: {r['status']}" for r in report if r["status"] != "unchanged"],
    )
    return int(ExitCode.OK)
