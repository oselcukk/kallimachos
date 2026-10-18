"""``schedule`` commands: install the daily/weekly jobs in the OS scheduler.

install | remove (alias uninstall) | status | run-missed. ``install`` renders the scheduler files,
shows them, and writes nothing without ``--yes`` (exit 5); it refuses while the e-mail channel is
unconfirmed (exit 5).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from kallimachos.cli_support import add_command, add_group
from kallimachos.config import load as config_load
from kallimachos.interfaces import ApprovalRequired, ExitCode, RunKind
from kallimachos.notify import email_smtp
from kallimachos.schedule import base as sched_base
from kallimachos.schedule import catchup
from kallimachos.state import runs as state_runs

BACKEND_CHOICES = [
    "local",
    "hermes",
    "github",
    "none",
    "launchd",
    "taskscheduler",
    "systemd",
    "cron",
]


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    g = add_group(sub, "schedule", help="OS/Hermes/GitHub scheduling", path="schedule")
    p = add_command(
        g,
        "install",
        cmd_install,
        help="render and install scheduler files",
        path="schedule install",
    )
    _common(p)
    p.add_argument("--python", metavar="PATH", help="interpreter the jobs run (default: this one)")
    p = add_command(
        g,
        "remove",
        cmd_remove,
        help="remove scheduler files",
        path="schedule remove",
        aliases=["uninstall"],
    )
    _common(p)
    p = add_command(
        g, "status", cmd_status, help="last run and exit code per job", path="schedule status"
    )
    _common(p)
    add_command(
        g,
        "run-missed",
        cmd_run_missed,
        help="catch up missed days (max schedule.max_catch_up_days)",
        path="schedule run-missed",
    )


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--backend", choices=BACKEND_CHOICES, help="scheduler (default: schedule.backend / by OS)"
    )
    p.add_argument(
        "--repo-dir",
        metavar="DIR",
        help="github backend: the (private) repository checkout to write the workflow into",
    )


def _flag(args: argparse.Namespace, name: str) -> bool:
    return bool(getattr(args, name, False))


def _rerun_hint(args: argparse.Namespace, command: str) -> str:
    """The exact command to rerun after approval, options included (``--backend``,
    ``--repo-dir``, ``--python``), so the hint never drops what the agent passed."""
    parts = ["kallimachos", "--yes", "schedule", command]
    for opt in ("backend", "repo_dir", "python"):
        value = getattr(args, opt, None)
        if value:
            parts.extend([f"--{opt.replace('_', '-')}", str(value)])
    return "`" + " ".join(parts) + "`"


def _backend(args: argparse.Namespace) -> tuple[Any, Any, sched_base.Backend]:
    config, layout = config_load.load_config(data_dir=getattr(args, "data_dir", None))
    repo = getattr(args, "repo_dir", None)
    backend = sched_base.get_backend(
        config,
        backend=getattr(args, "backend", None),
        repo_dir=Path(repo).expanduser() if repo else None,
    )
    return config, layout, backend


def cmd_install(args: argparse.Namespace) -> int:
    """Backend.render -> show -> approval (--yes) -> install; exit 5 without e-mail confirmation."""
    config, layout, backend = _backend(args)
    email_smtp.ensure_email_confirmed(config)  # ApprovalRequired -> exit 5
    python = getattr(args, "python", None) or sched_base.default_python()
    files = backend.render(config, layout, python=python)
    guidance = backend.guidance(config, layout, python=python)
    dry = _flag(args, "dry_run")
    approved = _flag(args, "yes")
    if _flag(args, "json"):
        print(
            json.dumps(
                {
                    "backend": backend.name,
                    "files": [
                        {"path": str(f.path), "job": f.job, "content": f.content} for f in files
                    ],
                    "guidance": guidance,
                    "installed": approved and not dry,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"Scheduler: {backend.name}. Files to write:")
        for f in files:
            exists = " (exists, will be replaced)" if f.path.exists() else ""
            print(f"\n--- {f.path} [{f.job}]{exists}\n{f.content.rstrip()}")
        if guidance:
            print("\nNotes:")
            print("\n".join(f"  {line}" for line in guidance))
    if dry:
        print("kallimachos: dry run, nothing written")
        return int(ExitCode.OK)
    if not approved:
        raise ApprovalRequired(
            "show these files to the user; after the user approves, rerun "
            + _rerun_hint(args, "install")
        )
    backend.install(files)
    if not _flag(args, "json"):
        print(f"\nkallimachos: {backend.name} schedule installed ({len(files)} file(s))")
    return int(ExitCode.OK)


def cmd_remove(args: argparse.Namespace) -> int:
    """Backend.remove (only Kallimachos' own scheduler entries); lists them, needs --yes."""
    config, _layout, backend = _backend(args)
    installed = [s for s in backend.status(config) if s.installed]
    if not installed:
        print(f"kallimachos: no {backend.name} jobs installed")
        return int(ExitCode.OK)
    print(f"Scheduler: {backend.name}. Jobs to remove:")
    for s in installed:
        print(f"  {s.job}" + (f" ({s.detail})" if s.detail else ""))
    if _flag(args, "dry_run"):
        print("kallimachos: dry run, nothing removed")
        return int(ExitCode.OK)
    if not _flag(args, "yes"):
        raise ApprovalRequired("after the user approves, rerun " + _rerun_hint(args, "remove"))
    backend.remove(config)
    print(f"kallimachos: {backend.name} jobs removed")
    return int(ExitCode.OK)


def cmd_status(args: argparse.Namespace) -> int:
    """Backend.status plus the last successful daily/weekly run."""
    config, layout, backend = _backend(args)
    statuses = backend.status(config)
    last = {
        kind.value: state_runs.last_successful_run_date(layout, kind)
        for kind in (RunKind.DAILY, RunKind.WEEKLY)
    }
    if _flag(args, "json"):
        print(
            json.dumps(
                {
                    "backend": backend.name,
                    "jobs": [asdict(s) for s in statuses],
                    "last_success": {k: v.isoformat() if v else None for k, v in last.items()},
                },
                indent=2,
            )
        )
        return int(ExitCode.OK)
    print(f"Scheduler: {backend.name}")
    for s in statuses:
        state = "installed" if s.installed else "not installed"
        extra = []
        if s.last_run_at:
            extra.append(f"last run {s.last_run_at}")
        if s.last_exit_code is not None:
            extra.append(f"exit {s.last_exit_code}")
        if s.detail:
            extra.append(s.detail)
        print(f"  {s.job:8} {state}" + (f" - {'; '.join(extra)}" if extra else ""))
    for kind, day in last.items():
        print(f"  last successful {kind} run: {day.isoformat() if day else 'none'}")
    return int(ExitCode.OK)


RunCommand = Callable[[argparse.Namespace], int]


def _run_namespace(args: argparse.Namespace, step: catchup.CatchUpPlan, today: _dt.date) -> Any:
    return argparse.Namespace(
        json=_flag(args, "json"),
        dry_run=_flag(args, "dry_run"),
        data_dir=getattr(args, "data_dir", None),
        yes=_flag(args, "yes"),
        command_path=f"run {step.kind.value}",
        run_kind=step.kind.value,
        date=None if step.day == today else step.day.isoformat(),
        first_run=False,
        no_notify=False,
        no_model=False,
        rerun=False,
        window_days=step.window_days,  # read by cmd_run to widen the window (one merged run)
    )


def cmd_run_missed(
    args: argparse.Namespace,
    *,
    run_command: RunCommand | None = None,
    now: _dt.datetime | None = None,
) -> int:
    """schedule.catchup.plan -> one merged run per kind (window covers every missed day)."""
    config, layout = config_load.load_config(data_dir=getattr(args, "data_dir", None))
    if run_command is None:
        from kallimachos import cli_run

        run_command = cli_run.cmd_run
        moment = now or cli_run.now_in(config)
    else:
        moment = now or _dt.datetime.now().astimezone()
    steps = catchup.plan(layout, config, moment)
    if not steps:
        print("kallimachos: nothing to catch up")
        return int(ExitCode.OK)
    worst = int(ExitCode.OK)
    for step in steps:
        days = ", ".join(d.isoformat() for d in step.missed)
        print(
            f"kallimachos: catching up {step.kind.value} for {days} "
            f"(one run, {step.window_days}-day window)"
        )
        code = int(run_command(_run_namespace(args, step, moment.date())))
        if code != int(ExitCode.OK) and worst == int(ExitCode.OK):
            worst = code
    return worst
