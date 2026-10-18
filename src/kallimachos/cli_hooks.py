"""``hooks`` commands: optional Claude Code / Codex hooks.

install | uninstall | status for the user, plus the two commands the hooks themselves run:
``hooks session-start`` (status summary) and ``hooks guard`` (PreToolUse protection). Both read
the hook JSON from stdin, never fail (exit 0, silent on error) and print the documented hook JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from kallimachos.cli_support import add_command, add_group
from kallimachos.config import load as config_load
from kallimachos.hooks import settings as hs
from kallimachos.interfaces import ApprovalRequired, ExitCode, UsageError
from kallimachos.schedule.base import default_python


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    g = add_group(sub, "hooks", help="optional Claude Code / Codex hooks", path="hooks")
    p = add_command(
        g, "install", cmd_install, help="add the Kallimachos hooks", path="hooks install"
    )
    _where(p)
    p.add_argument("--python", metavar="PATH", help="interpreter the hooks run (default: this one)")
    p = add_command(
        g, "uninstall", cmd_uninstall, help="remove the Kallimachos hooks", path="hooks uninstall"
    )
    _where(p)
    p = add_command(
        g, "status", cmd_status, help="where the hooks are installed", path="hooks status"
    )
    p.add_argument("--project", metavar="DIR", help="project directory (default: current)")
    for name, handler, text in (
        ("session-start", cmd_session_start, "SessionStart hook: short status summary"),
        ("guard", cmd_guard, "PreToolUse hook: protect state files and managed blocks"),
    ):
        p = add_command(g, name, handler, help=text, path=f"hooks {name}")
        p.add_argument(
            "--harness",
            choices=list(hs.TARGETS),
            default="claude",
            help="which agent calls the hook; selects the input/output JSON shape",
        )


def _where(p: argparse.ArgumentParser) -> None:
    p.add_argument("--target", choices=list(hs.TARGETS), required=True, help="agent to configure")
    p.add_argument(
        "--scope",
        choices=["user", "project", "local"],
        required=True,
        help="user = every session; project = this project (shared file); local = this project, "
        "personal file (Claude Code only)",
    )
    p.add_argument("--project", metavar="DIR", help="project directory (default: current)")


def _flag(args: argparse.Namespace, name: str) -> bool:
    return bool(getattr(args, name, False))


def _project(args: argparse.Namespace) -> Path:
    raw = getattr(args, "project", None)
    return Path(raw).expanduser().resolve() if raw else Path.cwd()


def _record_update(layout: Any, plan_path: Path, entry: dict[str, str] | None) -> None:
    items = [i for i in hs.read_record(layout) if i.get("path") != str(plan_path)]
    if entry is not None:
        items.append(entry)
    hs.write_record(layout, items)


def cmd_install(args: argparse.Namespace) -> int:
    config, layout = config_load.load_config(data_dir=getattr(args, "data_dir", None))
    if not (config.hooks.session_start or config.hooks.guard):
        raise UsageError("hooks.session_start and hooks.guard are both false; nothing to install")
    plan = hs.plan_install(
        args.target,
        args.scope,
        python=getattr(args, "python", None) or default_python(),
        layout=layout,
        home=Path.home(),
        project=_project(args),
        session_start=config.hooks.session_start,
        guard=config.hooks.guard,
    )
    if plan.problem:
        raise UsageError(plan.problem)
    ours = {e: g for e, g in (plan.after.get("hooks") or {}).items() if e in plan.events}
    added = {
        e: [g for g in groups if any(hs.is_ours(h) for h in g.get("hooks", []))]
        for e, groups in ours.items()
    }
    if _flag(args, "json"):
        shown = {"path": str(plan.path), "add": added, "warnings": plan.warnings}
        print(json.dumps(shown, indent=2))
    else:
        state = "exists; a backup is written first" if plan.path.exists() else "new file"
        print(f"{args.target} hooks ({args.scope}) -> {plan.path} ({state})")
        print("Entries added (all other hooks in the file are kept as they are):")
        print(json.dumps(added, indent=2))
        for w in plan.warnings:
            print(f"Note: {w}")
    if _flag(args, "dry_run"):
        print("kallimachos: dry run, nothing written")
        return int(ExitCode.OK)
    if not _flag(args, "yes"):
        raise ApprovalRequired(
            "show this to the user; after approval rerun `kallimachos --yes hooks install ...`"
        )
    if plan.after == plan.before:
        _record_update(
            layout,
            plan.path,
            {
                "target": args.target,
                "scope": args.scope,
                "path": str(plan.path),
                "events": ",".join(plan.events),
            },
        )
        print(f"kallimachos: hooks already installed in {plan.path}; nothing changed")
        return int(ExitCode.OK)
    saved = hs.backup(plan.path)
    written = hs.write_settings(plan.path, plan.after)
    _record_update(
        layout,
        plan.path,
        {
            "target": args.target,
            "scope": args.scope,
            "path": str(plan.path),
            "events": ",".join(plan.events),
        },
    )
    where = str(plan.path) if written == plan.path else f"{plan.path} -> {written}"
    print(f"kallimachos: hooks installed in {where}" + (f" (backup {saved})" if saved else ""))
    return int(ExitCode.OK)


def cmd_uninstall(args: argparse.Namespace) -> int:
    _config, layout = config_load.load_config(data_dir=getattr(args, "data_dir", None))
    path = hs.settings_path(args.target, args.scope, home=Path.home(), project=_project(args))
    data = hs.read_settings(path)
    after, removed = hs.strip_ours(data)
    if not removed:
        print(f"kallimachos: no Kallimachos hooks in {path}")
        _record_update(layout, path, None)
        return int(ExitCode.OK)
    print(f"kallimachos: {removed} Kallimachos handler(s) in {path}; other hooks stay")
    if _flag(args, "dry_run"):
        print("kallimachos: dry run, nothing removed")
        return int(ExitCode.OK)
    if not _flag(args, "yes"):
        raise ApprovalRequired(
            "after the user approves, rerun `kallimachos --yes hooks uninstall ...`"
        )
    saved = hs.backup(path)
    hs.write_settings(path, after)
    _record_update(layout, path, None)
    print(f"kallimachos: removed {removed} handler(s) from {path} (backup {saved})")
    return int(ExitCode.OK)


def cmd_status(args: argparse.Namespace) -> int:
    home, project = Path.home(), _project(args)
    rows: list[dict[str, Any]] = []
    for target in hs.TARGETS:
        for scope in ("user", "project", "local"):
            if target == "codex" and scope == "local":
                continue
            path = hs.settings_path(target, scope, home=home, project=project)
            try:
                events = hs.installed_events(hs.read_settings(path))
                problem = None
            except UsageError as exc:
                events, problem = [], str(exc)
            rows.append(
                {
                    "target": target,
                    "scope": scope,
                    "path": str(path),
                    "events": events,
                    "problem": problem,
                }
            )
    if _flag(args, "json"):
        print(json.dumps(rows, indent=2))
        return int(ExitCode.OK)
    for r in rows:
        what = ", ".join(r["events"]) if r["events"] else (r["problem"] or "not installed")
        print(f"  {r['target']:6} {r['scope']:7} {what}  ({r['path']})")
    print("Run `kallimachos doctor` to test the installed hooks.")
    return int(ExitCode.OK)


def _stdin_json() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def cmd_session_start(args: argparse.Namespace) -> int:
    """Print the summary as SessionStart ``additionalContext``; silent on any problem."""
    try:
        _stdin_json()
        from kallimachos.hooks.summary import build_summary

        config, layout = config_load.load_config(data_dir=getattr(args, "data_dir", None))
        if not config.hooks.session_start:
            return int(ExitCode.OK)
        text = build_summary(layout, config)
    except Exception:  # a hook must never break the session
        return int(ExitCode.OK)
    if text:
        out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
        print(json.dumps(out, ensure_ascii=False))
    return int(ExitCode.OK)


def cmd_guard(args: argparse.Namespace) -> int:
    """Deny file-tool writes to state files and managed blocks; allow everything else."""
    try:
        payload = _stdin_json()
        from kallimachos.hooks.guard import decide, deny_output

        config, layout = config_load.load_config(data_dir=getattr(args, "data_dir", None))
        if not config.hooks.guard:
            return int(ExitCode.OK)
        reason = decide(payload, layout)
    except Exception:  # fail open
        return int(ExitCode.OK)
    if reason:
        print(json.dumps(deny_output(reason), ensure_ascii=False))
    return int(ExitCode.OK)
