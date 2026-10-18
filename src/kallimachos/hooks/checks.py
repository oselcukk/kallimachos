"""``doctor`` checks for the agent hooks.

For every installation recorded by ``hooks install`` (``state/hooks.json``): the settings file
still holds the Kallimachos handlers, the interpreter in the command exists, and the two commands
actually work: ``session-start`` exits 0 within the budget, ``guard`` denies a write to a state
file and allows an unrelated one.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from kallimachos.config.schema import Config
from kallimachos.doctor import DoctorCheck
from kallimachos.hooks import settings as hook_settings
from kallimachos.interfaces import UsageError
from kallimachos.paths import DataLayout

Executor = Callable[[Sequence[str], str], "subprocess.CompletedProcess[str]"]


def default_executor(argv: Sequence[str], stdin: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv), input=stdin, capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))


def _split(command: str) -> list[str]:
    if sys.platform == "win32":  # list2cmdline quoting: non-POSIX split, then drop the quotes
        return [part.strip('"') for part in shlex.split(command, posix=False)]
    return shlex.split(command)


def _commands(data: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for event, groups in (data.get("hooks") or {}).items():
        for group in groups if isinstance(groups, list) else []:
            for h in group.get("hooks", []) if isinstance(group, dict) else []:
                if hook_settings.is_ours(h):
                    out[event] = str(h.get("command"))
    return out


def live_test(
    command: str, event: str, layout: DataLayout, executor: Executor, limit: int
) -> str | None:
    """None when the hook command behaves, else the problem."""
    argv = _split(command)
    if not argv or not Path(argv[0]).exists():
        return f"interpreter {argv[0] if argv else '?'} not found (reinstall the hooks)"
    if event == hook_settings.EVENT_SESSION:
        res = executor(argv, json.dumps({"hook_event_name": "SessionStart", "source": "startup"}))
        if res.returncode != 0:
            return f"session-start exited {res.returncode}"
        if res.stdout.strip():
            try:
                ctx = json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]
            except (ValueError, KeyError, TypeError):
                return "session-start printed something that is not hook JSON"
            if len(ctx) > limit:
                return f"session-start output {len(ctx)} > {limit} characters"
        return None
    probe = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(layout.seen_file), "content": "{}\n"},
    }
    res = executor(argv, json.dumps(probe))
    if res.returncode != 0:
        return f"guard exited {res.returncode}"
    try:
        decision = json.loads(res.stdout)["hookSpecificOutput"]["permissionDecision"]
    except (ValueError, KeyError, TypeError):
        return "guard did not deny a write to state/seen.jsonl"
    if decision != "deny":
        return "guard did not deny a write to state/seen.jsonl"
    other = {"file_path": str(layout.root.parent / "x.txt"), "content": ""}
    harmless = dict(probe, tool_input=other)
    res = executor(argv, json.dumps(harmless))
    if res.returncode != 0 or "deny" in res.stdout:
        return "guard blocks an unrelated file"
    return None


def doctor_checks(
    layout: DataLayout,
    config: Config | None,
    *,
    executor: Executor | None = None,
) -> list[DoctorCheck]:
    records = hook_settings.read_record(layout)
    if not records:
        return [DoctorCheck("hooks", True, "not installed (optional: `kallimachos hooks install`)")]
    run = executor or default_executor
    limit = config.hooks.summary_max_chars if config is not None else 600
    checks: list[DoctorCheck] = []
    for rec in records:
        name = f"hooks {rec.get('target')}:{rec.get('scope')}"
        path = Path(rec["path"])
        try:
            data = hook_settings.read_settings(path)
        except UsageError as exc:
            checks.append(DoctorCheck(name, False, str(exc)))
            continue
        cmds = _commands(data)
        expected = [e for e in str(rec.get("events", "")).split(",") if e]
        missing = [e for e in expected if e not in cmds]
        if missing:
            checks.append(
                DoctorCheck(
                    name,
                    False,
                    f"{path}: {', '.join(missing)} handler missing; rerun `kallimachos hooks "
                    "install`",
                )
            )
            continue
        problems = [
            f"{event}: {p}"
            for event, cmd in sorted(cmds.items())
            if (p := live_test(cmd, event, layout, run, limit)) is not None
        ]
        if problems:
            checks.append(DoctorCheck(name, False, "; ".join(problems)))
        else:
            checks.append(DoctorCheck(name, True, f"{path}: {', '.join(sorted(cmds))} working"))
    return checks
