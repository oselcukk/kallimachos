"""Avenox (beyin.py) detection and calls.

Kallimachos never writes companion/knowledge/tasks/receipts folders with file tools; it calls
``sync``, ``task-create`` and tells the agent how to send the single ``receipt``.

Facts taken from the Avenox source (read-only; vault-local ``beyin.py`` wrapper):

* the wrapper is run from the vault root as ``python3 beyin.py <command>``; it injects
  ``--vault``/``--state`` itself and prints JSON when ``--json`` is given or stdout is not a tty;
* ``sync`` returns ``{"status": "succeeded"|"degraded"|"conflict", "indexed", "deleted",
  "warnings", "conflicts"}``;
* ``task-create --file`` returns the stored record (``id``, ``kind``, ``revision``, ``status``,
  ``owner``, ``source`` ...) plus ``source_sync``; it refuses an existing source or id
  (``task source or id exists; use task-update``);
* errors are one JSON object on stderr ``{"error": <type>, "message": <text>}`` with exit 1;
* ``<state>/index.json`` holds ``{"excluded_dirs": [...]}`` (bare directory names); directories
  whose name contains a sensitive word are excluded from the index too.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kallimachos.records.normalize import nfc

RUNTIME_FILE = ".beyin-runtime.json"
VERSION_FILE = ".beyin-version"
RECEIPT_HARNESSES: tuple[str, ...] = (
    "codex",
    "claude",
    "antigravity",
    "hermes",
    "opencode",
    "omp",
    "kiro",
)

# Mirror of Avenox's sensitive directory words (ASCII-folded); a folder whose name carries one of
# these words never enters the Avenox index, so literature notes there would be invisible.
_SENSITIVE_WORDS = re.compile(
    r"(?:kasa|sifre|parola|kimlik|kimlig|finans|finansal|musteri|vergi|fatura|maas|ozel|gizli|"
    r"gizlilik)(?:ler|lar)?(?:i|im|in|imiz|leri|lari)?"
    r"|(?:private|secret|password|credential)s?"
)
# code points, not literals: s-cedilla, g-breve, u/o-umlaut, c-cedilla, dotless i, a/i/u-circumflex
_FOLD = str.maketrans(
    {
        chr(0x015F): "s",
        chr(0x011F): "g",
        chr(0x00FC): "u",
        chr(0x00F6): "o",
        chr(0x00E7): "c",
        chr(0x0131): "i",
        chr(0x00E2): "a",
        chr(0x00EE): "i",
        chr(0x00FB): "u",
    }
)


@dataclass
class AvenoxInfo:
    vault: Path
    beyin_py: Path
    runtime_json: Path
    version: str | None = None


def python_command() -> list[str]:
    """``["py", "-3"]`` on Windows, ``["python3"]`` elsewhere (AGENTS.md rule)."""
    return ["py", "-3"] if sys.platform.startswith("win") else ["python3"]


def detect(vault_path: Path) -> AvenoxInfo | None:
    """``beyin.py`` and ``.beyin-runtime.json`` present -> info, else ``None``."""
    vault = Path(vault_path).expanduser()
    beyin_py = vault / "beyin.py"
    runtime = vault / RUNTIME_FILE
    if not beyin_py.is_file() or not runtime.is_file():
        return None
    version: str | None = None
    stamp = vault / VERSION_FILE
    if stamp.is_file():
        try:
            version = stamp.read_text(encoding="utf-8").strip() or None
        except OSError:
            version = None
    return AvenoxInfo(vault=vault, beyin_py=beyin_py, runtime_json=runtime, version=version)


def command(info: AvenoxInfo, *args: str) -> list[str]:
    """Full command line for ``beyin.py <args>`` with JSON output forced."""
    return [*python_command(), str(info.beyin_py), "--json", *args]


def _parse_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else {"result": data}


def run(info: AvenoxInfo, *args: str, timeout: float = 300.0) -> dict[str, Any]:
    """Run ``beyin.py`` in the vault and return its JSON result.

    A failed command (non-zero exit) returns ``{"status": "error", "error": <type>, "message":
    <text>, "exit_code": n}`` built from Avenox's stderr JSON; callers decide the exit code.
    """
    try:
        proc = subprocess.run(
            command(info, *args),
            cwd=str(info.vault),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "error": type(exc).__name__, "message": str(exc)}
    if proc.returncode != 0:
        err = _parse_json(proc.stderr) or {}
        return {
            "status": "error",
            "error": str(err.get("error", "CommandFailed")),
            "message": str(err.get("message", proc.stderr.strip() or proc.stdout.strip())),
            "exit_code": proc.returncode,
        }
    result = _parse_json(proc.stdout)
    if result is None:
        return {"status": "error", "error": "NoJson", "message": proc.stdout.strip()[:500]}
    return result


def sync(info: AvenoxInfo) -> dict[str, Any]:
    """Run ``beyin.py sync``; ``conflict`` in the result makes ``note-write`` exit 3."""
    return run(info, "sync")


def task_create(info: AvenoxInfo, task_file: Path) -> dict[str, Any]:
    """``beyin.py task-create --file``; returns id/status/owner/source read back by Avenox."""
    return run(info, "task-create", "--file", str(task_file))


def task_exists(info: AvenoxInfo, task_id: str) -> bool:
    """True when ``tasks/<task_id>.md`` already exists in the vault (never recreated)."""
    return (info.vault / "tasks" / f"{task_id}.md").is_file()


def vault_relative(info: AvenoxInfo, path: Path) -> str:
    """POSIX path of ``path`` relative to the vault (receipt ``--ref`` form)."""
    resolved = Path(path)
    if not resolved.is_absolute():
        return resolved.as_posix()
    try:
        return resolved.resolve().relative_to(info.vault.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def receipt_command(
    info: AvenoxInfo,
    *,
    harness: str,
    event_id: str,
    summary_file: Path,
    refs: list[Path],
    session: str | None,
) -> list[str]:
    """Command line the agent runs itself at session end (one receipt; no CLI wrapper).

    Mirrors the Avenox beyin skill: ``receipt --harness H --event-id ID --summary-file PATH
    --ref R [--ref R ...] [--session S]``; refs are vault-relative existing files; ``--session``
    only when the hook supplied a value.
    """
    if harness not in RECEIPT_HARNESSES:
        raise ValueError(f"unknown receipt harness {harness!r}; one of {RECEIPT_HARNESSES}")
    if not event_id.strip():
        raise ValueError("event_id must not be empty")
    argv = [
        *python_command(),
        str(info.beyin_py),
        "receipt",
        "--harness",
        harness,
        "--event-id",
        event_id,
        "--summary-file",
        str(summary_file),
    ]
    for ref in refs:
        argv.extend(["--ref", vault_relative(info, ref)])
    if session:
        argv.extend(["--session", session])
    return argv


def _fold(name: str) -> str:
    # capital dotted I (U+0130) and the combining dot above (U+0307) fold to plain i
    text = nfc(name).replace(chr(0x0130), "i").replace("I", "i").lower()
    return nfc(text.replace(chr(0x0307), "")).translate(_FOLD)


def sensitive_name(name: str) -> bool:
    """True when a directory name carries an Avenox sensitive word (excluded from the index)."""
    return any(_SENSITIVE_WORDS.fullmatch(word) for word in re.findall(r"[^\W\d_]+", _fold(name)))


def state_dir(info: AvenoxInfo) -> Path | None:
    """Avenox local state directory from ``.beyin-runtime.json`` (``state`` key), if usable."""
    try:
        data = json.loads(info.runtime_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    raw = data.get("state") if isinstance(data, dict) else None
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_absolute() else None


def excluded_dirs(info: AvenoxInfo) -> list[str]:
    """``excluded_dirs`` of ``<state>/index.json`` (empty when the file is absent)."""
    state = state_dir(info)
    if state is None:
        return []
    index = state / "index.json"
    if not index.is_file():
        return []
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    names = data.get("excluded_dirs", []) if isinstance(data, dict) else []
    return [str(n) for n in names] if isinstance(names, list) else []


def literature_folder_indexed(info: AvenoxInfo, literature_folder: str) -> tuple[bool, str]:
    """``doctor`` check: folder not in ``index.json excluded_dirs`` / sensitive names."""
    parts = [p for p in Path(literature_folder).parts if p not in ("", ".", "/")]
    if not parts:
        return False, "literature folder is empty"
    if ".." in parts:
        return False, "literature folder must stay inside the vault"
    excluded = set(excluded_dirs(info))
    for part in parts:
        if part in excluded:
            return False, f"{part!r} is listed in Avenox index.json excluded_dirs"
        if sensitive_name(part):
            return False, f"{part!r} is a sensitive folder name; Avenox will not index it"
    if parts[0] == "notes":
        return True, "folder is under Avenox's own notes/ area (allowed, not recommended)"
    return True, "indexed"
