"""``archive`` commands: open-access PDF download and catalog maintenance.

run | retry | status | test | scan-existing ROOT | confirm --sha.

Each handler accepts a keyword-only ``context`` (an :class:`ArchiveContext`) so tests and other
commands can inject config, HTTP, sources and state; without it the context is built from the
data directory (``--data-dir``), ``config.yaml`` and ``profile.yaml``.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kallimachos.archive import triggers as trig
from kallimachos.archive.catalog import Catalog, UnreachableList
from kallimachos.archive.existing import scan_existing
from kallimachos.archive.pipeline import (
    ArchiveContext,
    ArchiveReport,
    rule_chain_filter,
    run_archive,
    run_retry,
)
from kallimachos.cli_support import add_command, add_group
from kallimachos.interfaces import (
    ExitCode,
    FeedbackEntry,
    RunResult,
    Source,
    UsageError,
    build,
    dump,
)
from kallimachos.paths import DataLayout
from kallimachos.state import store

ARCHIVE_SOURCES = ("openalex", "arxiv", "iacr_eprint", "crossref")
RUN_LOOKBACK_DAYS = 14


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    g = add_group(
        sub, "archive", help="download open-access PDFs and maintain the catalog", path="archive"
    )
    add_command(g, "run", cmd_run, help="process triggers and download", path="archive run")
    add_command(g, "retry", cmd_retry, help="retry open unreachable entries", path="archive retry")
    add_command(
        g, "status", cmd_status, help="catalog and unreachable summary", path="archive status"
    )
    add_command(
        g,
        "test",
        cmd_test,
        help="one small request against an open-access endpoint and an archive-root check",
        path="archive test",
    )
    p = add_command(
        g,
        "scan-existing",
        cmd_scan_existing,
        help="read-only scan of an existing PDF library",
        path="archive scan-existing",
    )
    p.add_argument("root", metavar="ROOT", help="folder of an existing PDF library to inventory")
    p = add_command(
        g,
        "confirm",
        cmd_confirm,
        help="bridge: mark an outgoing file delivered",
        path="archive confirm",
    )
    p.add_argument(
        "--sha",
        required=True,
        metavar="SHA256",
        help="sha256 of the delivered file (from bridge pull)",
    )


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------


def _flag(args: argparse.Namespace, name: str) -> bool:
    return bool(getattr(args, name, False))


def build_context(args: argparse.Namespace, *, with_network: bool = True) -> ArchiveContext:
    """Default context: config + profile + weights from the data directory, the shared Http
    client and the registered sources (L1/L2)."""
    from kallimachos.config.load import load_config
    from kallimachos.feedback.weights import load_weights
    from kallimachos.http.client import from_config
    from kallimachos.profile.load import load_profile
    from kallimachos.secrets import resolve_ref
    from kallimachos.sources.registry import get_source
    from kallimachos.state.lock import FileLock

    config, layout = load_config(data_dir=getattr(args, "data_dir", None))
    profile = load_profile(layout.profile_file, missing_ok=True)
    weights = load_weights(layout)
    # unknown PDF hosts (publisher mirrors) are paced at 2 s; registry/PDF hosts keep their floors
    http = from_config(config, layout, default_interval_s=2.0)
    sources: dict[str, Source] = {}
    if with_network:
        for name in ARCHIVE_SOURCES:
            with contextlib.suppress(Exception):
                sources[name] = get_source(name, config)
    return ArchiveContext(
        config=config,
        layout=layout,
        profile=profile,
        http=http,
        sources=sources,
        weights=weights,
        keep=rule_chain_filter(profile, weights),
        resolve_email=resolve_ref,
        lock=FileLock(layout.lock_file("archive"), owner="archive"),
    )


def load_recent_runs(layout: DataLayout, *, days: int = RUN_LOOKBACK_DAYS) -> list[RunResult]:
    """Run files of the last ``days`` days (triggers: auto_pass, always_show_new)."""
    if not layout.runs.is_dir():
        return []
    cutoff = _dt.date.today() - _dt.timedelta(days=days)
    out: list[RunResult] = []
    for path in sorted(layout.runs.glob("*.json")):
        stem = path.name[: -len(".json")]
        if "." in stem or stem[:10] < cutoff.isoformat():
            continue
        data = store.read_json(path, default=None)
        if data:
            out.append(build(RunResult, data))
    return out


def load_feedback_entries(layout: DataLayout) -> list[FeedbackEntry]:
    return [build(FeedbackEntry, obj) for obj in store.read_jsonl(layout.feedback_file)]


def _emit(args: argparse.Namespace, payload: dict[str, Any], lines: Sequence[str]) -> None:
    if _flag(args, "json"):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for line in lines:
            print(line)


def _report_payload(report: ArchiveReport, dry_run: bool) -> dict[str, Any]:
    payload: dict[str, Any] = dump(report)
    payload["dry_run"] = dry_run
    payload["summary"] = report.summary_line()
    return payload


def _report_lines(report: ArchiveReport, dry_run: bool) -> list[str]:
    if dry_run:
        head = f"dry run: {len(report.planned)} to look up, {report.skipped_seen} already seen"
        return [head, *(f"  {p}" for p in report.planned), "nothing was written"]
    lines = [report.summary_line() or "archive: nothing new"]
    lines += [f"  stored {p}" for p in report.stored_paths]
    lines += [f"  superseded preprint {a} -> {b} (kept, not deleted)" for a, b in report.superseded]
    lines += [f"warning: {e}" for e in report.errors]
    return lines


def _exit(report: ArchiveReport) -> int:
    return int(ExitCode.PARTIAL) if report.errors else int(ExitCode.OK)


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace, *, context: ArchiveContext | None = None) -> int:
    """Collect triggers, resolve metadata, find an open-access PDF, download, name and catalog;
    --dry-run writes nothing."""
    ctx = context or build_context(args)
    if not ctx.config.archive.enabled:
        raise UsageError("archive.enabled is false in config.yaml")
    _ = ctx.root  # archive.root must be set
    dry_run = _flag(args, "dry_run")
    errors: list[str] = []
    with ctx.lock or contextlib.nullcontext():
        feedback = ctx.feedback if ctx.feedback is not None else load_feedback_entries(ctx.layout)
        runs = ctx.runs if ctx.runs is not None else load_recent_runs(ctx.layout)
        triggers = trig.collect_triggers(ctx.layout, feedback, runs, ctx.profile, errors=errors)
        report = run_archive(ctx, triggers, dry_run=dry_run)
    report.errors = [*errors, *report.errors]
    _emit(args, _report_payload(report, dry_run), _report_lines(report, dry_run))
    return _exit(report)


def cmd_retry(args: argparse.Namespace, *, context: ArchiveContext | None = None) -> int:
    """Retry open unreachable entries under the retry schedule (bounded attempts)."""
    ctx = context or build_context(args)
    _ = ctx.root
    dry_run = _flag(args, "dry_run")
    with ctx.lock or contextlib.nullcontext():
        report = run_retry(ctx, dry_run=dry_run)
    lines = _report_lines(report, dry_run)
    lines.append(f"retried {report.retried}, left alone (403/HTML) {report.retry_skipped}")
    if ctx.pacer.dropped:
        lines.append("hosts dropped after repeated 429: " + ", ".join(sorted(ctx.pacer.dropped)))
    _emit(args, _report_payload(report, dry_run), lines)
    return _exit(report)


def cmd_test(args: argparse.Namespace, *, context: ArchiveContext | None = None) -> int:
    """One small request against the first enabled open-access source the archive downloads
    from (arXiv preferred) plus a check that ``archive.root`` is set and writable. Exit 6 when the
    request fails, 2 when the archive is not configured, 3 when only the root check fails."""
    import os

    from kallimachos.sources.registry import get_source

    if context is not None:
        config, layout = context.config, context.layout
        http: Any = context.http
    else:
        from kallimachos.config.load import load_config
        from kallimachos.http.client import from_config

        config, layout = load_config(data_dir=getattr(args, "data_dir", None))
        http = from_config(config, layout)
    enabled = [
        name
        for name in ("arxiv", "iacr_eprint", "openalex", "crossref")
        if getattr(getattr(config.sources, name, None), "enabled", False)
    ]
    if not enabled:
        raise UsageError("no open-access source is enabled (sources.arxiv/openalex/...)")
    source = get_source(enabled[0], config)
    result = source.test(http)
    root = Path(config.archive.root).expanduser() if config.archive.root else None
    probe = root
    while probe is not None and not probe.exists() and probe != probe.parent:
        probe = probe.parent
    root_ok = bool(root) and probe is not None and os.access(probe, os.W_OK)
    root_msg = (
        f"archive.root {root} is writable"
        if root_ok
        else ("archive.root is not set" if root is None else f"archive.root {root} is not writable")
    )
    payload = {
        "source": source.name,
        "ok": result.ok,
        "message": result.message,
        "http_status": result.http_status,
        "latency_s": result.latency_s,
        "archive_root_ok": root_ok,
        "archive_root": str(root) if root else None,
    }
    lines = [
        f"{source.name}: {'ok' if result.ok else 'FAILED'}"
        + (f" HTTP {result.http_status}" if result.http_status is not None else "")
        + f": {result.message}",
        f"archive root: {'ok' if root_ok else 'FAILED'}: {root_msg}",
    ]
    _emit(args, payload, lines)
    if not result.ok:
        return int(ExitCode.EXHAUSTED)
    return int(ExitCode.OK if root_ok else ExitCode.PARTIAL)


def cmd_status(args: argparse.Namespace, *, context: ArchiveContext | None = None) -> int:
    """Counts by status, last log line."""
    if context is not None:
        layout = context.layout
    else:
        from kallimachos.config.load import load_config

        layout = load_config(data_dir=getattr(args, "data_dir", None))[1]
    catalog = Catalog.load(layout.catalog_file)
    unreachable = UnreachableList.load(layout.unreachable_file)
    by_status: dict[str, int] = {}
    for entry in catalog.entries():
        by_status[entry.status.value] = by_status.get(entry.status.value, 0) + 1
    un_status: dict[str, int] = {}
    for u in unreachable.entries():
        un_status[u.status] = un_status.get(u.status, 0) + 1
    last: dict[str, Any] | None = None
    for row in store.read_jsonl(layout.archive_log_file):
        last = row
    payload = {"catalog": by_status, "unreachable": un_status, "last_log": last}
    lines = [
        "catalog: " + (", ".join(f"{k} {v}" for k, v in sorted(by_status.items())) or "empty"),
        "unreachable: " + (", ".join(f"{k} {v}" for k, v in sorted(un_status.items())) or "none"),
        "last run: " + (f"{last['at']} {last['command']}" if last else "never"),
    ]
    _emit(args, payload, lines)
    return int(ExitCode.OK)


def cmd_scan_existing(args: argparse.Namespace, *, context: ArchiveContext | None = None) -> int:
    """Read-only inventory of an existing library; never moves files; the privacy gate runs
    before any metadata query."""
    ctx = context or build_context(args)
    root = Path(args.root).expanduser()
    if not root.is_dir():
        raise UsageError(f"not a directory: {root}")
    dry_run = _flag(args, "dry_run")
    report = scan_existing(
        root, ctx.layout, ctx.config, ctx.http, dry_run=dry_run, sources=ctx.sources
    )
    payload: dict[str, Any] = dump(report)
    payload["dry_run"] = dry_run
    lines = [
        f"scanned {report.scanned} PDFs (read-only; nothing was moved or renamed)",
        f"identified {report.identified}, looked up {report.queried}, cached {report.from_cache}",
        f"skipped: {report.skipped_sensitive} sensitive, {report.skipped_dataless} cloud-only",
        f"duplicates: {len(report.duplicates)}",
    ]
    if dry_run:
        lines.append("dry run: existing_catalog.jsonl and duplicates.md were not written")
    for err in report.errors:
        print(f"warning: {err}", file=sys.stderr)
    _emit(args, payload, lines)
    return int(ExitCode.PARTIAL) if report.errors else int(ExitCode.OK)


def cmd_confirm(args: argparse.Namespace) -> int:
    """Bridge: an outgoing file becomes stored once the computer confirms its sha256."""
    from kallimachos.bridge.deliver import confirm
    from kallimachos.config.load import load_config

    _config, layout = load_config(data_dir=args.data_dir)
    print(confirm(layout, args.sha, dry_run=bool(args.dry_run)))
    return 0
