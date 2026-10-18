"""``bridge`` commands: move archive files and catalog rows between a server and a computer.

push | pull | deliver | status | push-ack | reconcile (optional module).
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from kallimachos.bridge import deliver as bridge
from kallimachos.bridge.base import Transport
from kallimachos.bridge.catalog_copy import load_entries
from kallimachos.bridge.ssh_rsync import SshRsyncTransport
from kallimachos.cli_support import add_command, add_group
from kallimachos.config import load as config_load
from kallimachos.config.schema import Config
from kallimachos.interfaces import ExitCode, UsageError
from kallimachos.paths import DataLayout
from kallimachos.state.lock import FileLock


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    g = add_group(
        sub, "bridge", help="server -> computer delivery with sha verification", path="bridge"
    )
    add_command(
        g, "pull", cmd_pull, help="fetch outgoing PDFs and catalog copies", path="bridge pull"
    )
    add_command(
        g,
        "deliver",
        cmd_deliver,
        help="place verified PDFs into the archive",
        path="bridge deliver",
    )
    add_command(g, "push", cmd_push, help="alias of push-ack", path="bridge push")
    add_command(
        g,
        "push-ack",
        cmd_push_ack,
        help="confirm delivered files on the server",
        path="bridge push-ack",
    )
    add_command(g, "status", cmd_status, help="pending, delivered, conflicts", path="bridge status")
    add_command(
        g, "reconcile", cmd_reconcile, help="merge diverged catalog copies", path="bridge reconcile"
    )


def _context(args: argparse.Namespace) -> tuple[Config, DataLayout]:
    return config_load.load_config(data_dir=getattr(args, "data_dir", None))


def bridge_lock(layout: DataLayout, owner: str) -> FileLock:
    """One ``state/locks/bridge.lock`` for pull, deliver, push-ack and reconcile: two rounds never
    overlap (exit 4 when held)."""
    return FileLock(layout.lock_file("bridge"), owner=owner)


def make_transport(config: Config) -> Transport:
    """The configured transport (v1: ``mode: pull`` over ssh + rsync)."""
    b = config.bridge
    if not b.enabled:
        raise UsageError("the bridge is disabled (bridge.enabled: false)")
    if not b.remote or not b.remote_data_dir:
        raise UsageError("bridge.remote and bridge.remote_data_dir must be set")
    return SshRsyncTransport(b.remote, b.remote_data_dir, ssh_options=b.ssh_options, retry=b.retry)


def _emit(args: argparse.Namespace, data: dict[str, Any], lines: list[str]) -> None:
    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        for line in lines:
            print(line)


def _report_dict(report: bridge.DeliverReport) -> dict[str, Any]:
    return {
        "delivered": [str(p) for p in report.delivered],
        "already_present": [str(p) for p in report.already_present],
        "deferred_dataless": [str(p) for p in report.deferred_dataless],
        "moved": [[str(a), str(b)] for a, b in report.moved],
        "conflicts": report.conflicts,
        "rejected": report.rejected,
        "notes": report.notes,
    }


def cmd_pull(args: argparse.Namespace) -> int:
    """bridge.deliver.pull."""
    config, layout = _context(args)
    transport = make_transport(config)
    try:
        with bridge_lock(layout, "bridge pull"):
            files = bridge.pull(transport, layout, config, dry_run=bool(args.dry_run))
    finally:
        transport.close()
    state = bridge.read_state(layout)
    diverged = state.get("catalog") == "diverged" and not args.dry_run
    lines = [f"pulled: {len(files)} PDF(s) in the bridge cache"]
    if diverged:
        missing = state.get("catalog_missing_ids", [])
        lines.append(
            f"CATALOG_DIVERGED: {len(missing)} local catalog row(s) are missing on the server; "
            "the local copy was not updated. Run `kallimachos bridge reconcile`."
        )
    _emit(args, {"files": [str(f) for f in files], "state": state}, lines)
    return int(ExitCode.PARTIAL if diverged else ExitCode.OK)


def cmd_deliver(args: argparse.Namespace) -> int:
    """bridge.deliver.deliver; stops on CONFLICT."""
    config, layout = _context(args)
    if not config.bridge.enabled:
        raise UsageError("the bridge is disabled (bridge.enabled: false)")
    dry = bool(args.dry_run)
    with bridge_lock(layout, "bridge deliver"):
        report = bridge.deliver(layout, config, dry_run=dry)
        if not report.conflicts:
            report.merge(bridge.process_incoming(layout, config, dry_run=dry))
    lines = [
        f"delivered: {len(report.delivered)}, already present: {len(report.already_present)}, "
        f"moved by the user: {len(report.moved)}, "
        f"deferred (cloud-only): {len(report.deferred_dataless)}",
        *report.conflicts,
        *report.rejected,
        *report.notes,
    ]
    _emit(args, _report_dict(report), lines)
    return int(ExitCode.PARTIAL if report.conflicts else ExitCode.OK)


def cmd_push(args: argparse.Namespace) -> int:
    """Same as push-ack."""
    return cmd_push_ack(args)


def cmd_push_ack(args: argparse.Namespace) -> int:
    """bridge.deliver.push_ack."""
    config, layout = _context(args)
    if args.dry_run:
        pending = bridge.pending_acks(layout)
        _emit(args, {"pending": pending}, [f"would confirm {len(pending)} file(s)"])
        return int(ExitCode.OK)
    transport = make_transport(config)
    try:
        with bridge_lock(layout, "bridge push-ack"):
            acked = bridge.push_ack(transport, layout, config)
    finally:
        transport.close()
    _emit(args, {"acked": acked}, [f"confirmed on the server: {acked}"])
    return int(ExitCode.OK)


def cmd_status(args: argparse.Namespace) -> int:
    """Summary of bridge/incoming and delivered.jsonl."""
    _config, layout = _context(args)
    state = bridge.read_state(layout)
    pending = bridge.pending_acks(layout)
    cached = bridge.cached_pdfs(layout)
    outbox = load_entries(bridge.outbox_file(layout))
    data = {
        "cached_pdfs": len(cached),
        "delivered": len(bridge.delivered_shas(layout)),
        "pending_ack": len(pending),
        "outbox": len(outbox),
        **state,
    }
    lines = [
        f"cached PDFs: {data['cached_pdfs']}",
        f"delivered: {data['delivered']} (waiting for push-ack: {data['pending_ack']})",
        f"rows waiting for the server: {data['outbox']}",
        f"catalog copy: {state.get('catalog', 'never pulled')}",
        *[str(c) for c in state.get("conflicts", [])],
    ]
    _emit(args, data, lines)
    return int(ExitCode.OK)


def cmd_reconcile(args: argparse.Namespace) -> int:
    """bridge.deliver.reconcile."""
    config, layout = _context(args)
    with bridge_lock(layout, "bridge reconcile"):
        count = bridge.reconcile(layout, config)
    _emit(args, {"merged": count}, [f"merged rows: {count}"])
    return int(ExitCode.OK)
