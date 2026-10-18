"""Install / remove the Kallimachos hooks in Claude Code and Codex settings.

Facts used (official docs, read 2026-10-18):

* Claude Code (code.claude.com/docs/en/hooks): hooks live under ``"hooks"`` in
  ``~/.claude/settings.json`` (user), ``.claude/settings.json`` (project, shared) or
  ``.claude/settings.local.json`` (project, personal); event -> list of matcher groups -> list of
  ``{"type": "command", "command": ..., "timeout": seconds}`` handlers.
* Codex (developers.openai.com/codex/hooks): ``~/.codex/hooks.json`` (user) or
  ``<repo>/.codex/hooks.json`` (project, loaded only when the project layer is trusted); same
  three-level shape; hooks are on by default (``[features] hooks = false`` disables them); one
  layer uses either ``hooks.json`` or inline ``[hooks]`` tables in ``config.toml``, not both; new or
  changed hooks must be reviewed and trusted with ``/hooks`` before they run. File edits arrive as
  ``apply_patch`` (the ``Edit``/``Write`` matchers alias it).

Only handlers whose command runs ``kallimachos ... hooks session-start|guard`` are ever added or
removed; every other hook (an Avenox vault's, the user's own) and every other setting is kept
semantically unchanged (the file is re-serialised as 2-space-indented JSON, so byte layout may
differ). A uniquely named backup is written before a change and only when something changes; a
settings file that is a symlink is updated through its target, so a dotfiles checkout stays the
single source.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from kallimachos.interfaces import UsageError
from kallimachos.paths import DataLayout

Target = Literal["claude", "codex"]
Scope = Literal["user", "project", "local"]
TARGETS: tuple[Target, ...] = ("claude", "codex")

EVENT_SESSION = "SessionStart"
EVENT_GUARD = "PreToolUse"
SESSION_MATCHER = "startup|resume|clear|compact"
GUARD_MATCHER = {
    "claude": "Edit|Write|MultiEdit|NotebookEdit|Bash|PowerShell",
    "codex": "apply_patch|Edit|Write|Bash",
}
TIMEOUT_S = 10
_OURS = re.compile(r"kallimachos.*\bhooks (session-start|guard)\b")


def settings_path(target: Target, scope: Scope, *, home: Path, project: Path) -> Path:
    if target == "claude":
        if scope == "user":
            return home / ".claude" / "settings.json"
        if scope == "project":
            return project / ".claude" / "settings.json"
        return project / ".claude" / "settings.local.json"
    if scope == "local":
        raise UsageError("Codex has no 'local' hook scope; use user or project")
    base = home if scope == "user" else project
    return base / ".codex" / "hooks.json"


def _quote(args: list[str]) -> str:
    if sys.platform == "win32":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


def hook_command(python: str, layout: DataLayout, which: str, target: Target) -> str:
    return _quote(
        [
            python,
            "-m",
            "kallimachos",
            "--data-dir",
            str(layout.root),
            "hooks",
            which,
            "--harness",
            target,
        ]
    )


def is_ours(handler: Any) -> bool:
    return isinstance(handler, dict) and bool(_OURS.search(str(handler.get("command", ""))))


def our_groups(
    target: Target, python: str, layout: DataLayout, *, session_start: bool, guard: bool
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if session_start:
        out[EVENT_SESSION] = {
            "matcher": SESSION_MATCHER,
            "hooks": [
                {
                    "type": "command",
                    "command": hook_command(python, layout, "session-start", target),
                    "timeout": TIMEOUT_S,
                }
            ],
        }
    if guard:
        out[EVENT_GUARD] = {
            "matcher": GUARD_MATCHER[target],
            "hooks": [
                {
                    "type": "command",
                    "command": hook_command(python, layout, "guard", target),
                    "timeout": TIMEOUT_S,
                }
            ],
        }
    return out


def read_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError) as exc:
        raise UsageError(f"{path} is not valid JSON ({exc}); not touched") from None
    if not isinstance(data, dict):
        raise UsageError(f"{path} does not hold a JSON object; not touched")
    hooks = data.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise UsageError(f"{path}: 'hooks' is not an object; not touched")
    return data


def strip_ours(data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Copy of ``data`` without Kallimachos handlers; empty groups/events they leave are dropped.
    Returns the number of handlers removed."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return dict(data), 0
    removed = 0
    new_hooks: dict[str, Any] = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            new_hooks[event] = groups
            continue
        kept_groups: list[Any] = []
        for group in groups:
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(handlers, list):
                kept_groups.append(group)
                continue
            kept = [h for h in handlers if not is_ours(h)]
            removed += len(handlers) - len(kept)
            if kept:
                kept_groups.append({**group, "hooks": kept})
            elif len(kept) == len(handlers):  # an empty group that was not ours
                kept_groups.append(group)
        if kept_groups or not groups:
            new_hooks[event] = kept_groups
    out = dict(data)
    if new_hooks:
        out["hooks"] = new_hooks
    else:
        out.pop("hooks", None)
    return out, removed


def merged(data: dict[str, Any], groups: dict[str, dict[str, Any]]) -> dict[str, Any]:
    base, _n = strip_ours(data)
    hooks = dict(base.get("hooks") or {})
    for event, group in groups.items():
        hooks[event] = [*list(hooks.get(event) or []), group]
    base["hooks"] = hooks
    return base


def installed_events(data: dict[str, Any]) -> list[str]:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return []
    out: list[str] = []
    for event, groups in hooks.items():
        for group in groups if isinstance(groups, list) else []:
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if isinstance(handlers, list) and any(is_ours(h) for h in handlers):
                out.append(event)
                break
    return out


def backup(path: Path) -> Path | None:
    """``<name>.bak-kallimachos-<stamp>`` next to the real file; never overwrites an earlier
    backup (unique name through ``O_EXCL``). ``None`` when there is nothing to back up."""
    from kallimachos.state import store

    return store.backup_copy(path, label="bak-kallimachos")


def write_target(path: Path) -> Path:
    """The file actually written: the symlink target when ``path`` is a symlink."""
    if path.is_symlink():
        target = Path(os.path.realpath(path))
        if not target.exists():
            raise UsageError(f"{path} is a symlink to {target}, which does not exist; not touched")
        return target
    return path


def write_settings(path: Path, data: dict[str, Any]) -> Path:
    """Atomically write ``data`` as JSON to ``path`` (or to its symlink target). Returns the file
    written."""
    target = write_target(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".kallimachos-tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target


_INLINE_HOOKS = re.compile(r"^\s*\[\[?hooks[.\]]", re.M)
_FEATURE_OFF = re.compile(r"^\s*(hooks|codex_hooks)\s*=\s*false\b", re.M)


def codex_layer_notes(path: Path, *, user_scope: bool) -> tuple[str | None, list[str]]:
    """``(blocking_problem, warnings)`` for the Codex layer that holds ``path``."""
    toml = path.with_name("config.toml")
    warnings = [
        "Codex runs new or changed hooks only after you review and trust them with /hooks.",
    ]
    if not user_scope:
        warnings.append("Project hooks load only when this project's .codex/ layer is trusted.")
    try:
        text = toml.read_text(encoding="utf-8")
    except OSError:
        return None, warnings
    if _INLINE_HOOKS.search(text):
        return (
            f"{toml} defines inline [hooks] tables; Codex accepts either hooks.json or inline "
            "tables in one layer, not both. Add the Kallimachos handlers there by hand "
            "(see docs/hooks.md) or use the other scope.",
            warnings,
        )
    if _FEATURE_OFF.search(text):
        warnings.append(f"{toml} turns hooks off ([features] hooks = false); they will not run.")
    return None, warnings


@dataclass
class HookPlan:
    target: Target
    scope: Scope
    path: Path
    before: dict[str, Any]
    after: dict[str, Any]
    events: list[str]
    warnings: list[str] = field(default_factory=list)
    problem: str | None = None


def plan_install(
    target: Target,
    scope: Scope,
    *,
    python: str,
    layout: DataLayout,
    home: Path,
    project: Path,
    session_start: bool = True,
    guard: bool = True,
) -> HookPlan:
    path = settings_path(target, scope, home=home, project=project)
    before = read_settings(path)
    groups = our_groups(target, python, layout, session_start=session_start, guard=guard)
    plan = HookPlan(target, scope, path, before, merged(before, groups), list(groups))
    if target == "codex":
        plan.problem, plan.warnings = codex_layer_notes(path, user_scope=scope == "user")
    elif scope == "project":
        plan.warnings.append(
            ".claude/settings.json is shared with everyone who clones the project; "
            "settings.local.json (scope local) stays personal."
        )
    return plan


def record_file(layout: DataLayout) -> Path:
    return layout.state / "hooks.json"


def read_record(layout: DataLayout) -> list[dict[str, str]]:
    try:
        data = json.loads(record_file(layout).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("installed", []) if isinstance(data, dict) else []
    return [i for i in items if isinstance(i, dict) and "path" in i]


def write_record(layout: DataLayout, items: list[dict[str, str]]) -> None:
    from kallimachos.state import store

    record_file(layout).parent.mkdir(parents=True, exist_ok=True)
    data = {"schema": "kallimachos.hooks/1", "installed": items}
    store.write_json_atomic(record_file(layout), data)
