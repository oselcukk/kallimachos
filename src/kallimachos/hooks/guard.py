"""PreToolUse guard.

State files, the catalog and the managed blocks of notes (``<!-- kallimachos:begin KIND -->`` ...
``<!-- kallimachos:end KIND -->``) change only through ``kallimachos`` commands, which validate,
log and keep the state machine consistent. When an agent tries to change them with a file tool
(Claude Code ``Edit``/``Write``/``MultiEdit``, Codex ``apply_patch``) or an obvious shell write, the
guard denies the call and names the right command.

The guard is fail-open: any unexpected input or error allows the call (the hook must never break a
session). It reads the target file only when it is small and only to find managed blocks.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kallimachos.paths import DataLayout

MAX_READ_BYTES = 2_000_000
MARKER = "<!-- kallimachos:"
# the daily Markdown file: ticking its boxes is the Markdown feedback channel
EXEMPT_KINDS = frozenset({"daily"})

_BEGIN = re.compile(r"<!-- kallimachos:begin (?P<kind>[A-Za-z0-9_-]+)(?:\s[^>]*?)?\s*-->")

# shell commands whose non-option arguments are files they change
_WRITE_VERBS = frozenset(
    {
        "tee",
        "rm",
        "mv",
        "truncate",
        "dd",
        "del",
        "install",
        "shred",
        "unlink",
        "rmdir",
        "ln",
        "set-content",
        "add-content",
        "out-file",
        "remove-item",
        "move-item",
        "copy-item",
        "clear-content",
    }
)
_COPY_VERBS = frozenset({"cp", "copy-item", "install"})  # only the destination is a write
_INPLACE_EDITORS = frozenset({"sed", "perl", "gsed"})
_INPLACE_OPT = re.compile(r"^-[A-Za-z0-9]*i|^--in-place")
_INTERPRETERS = frozenset({"python", "python3", "python2", "perl", "ruby", "node", "php", "lua"})
_INLINE_CODE_OPTS = frozenset({"-c", "-e", "-E", "-r"})
_INLINE_WRITE = re.compile(
    r"""['"](?:w|a|wb|ab|w\+|a\+|r\+|rb\+|wt|at)['"]|\.write\(|write_text|write_bytes|"""
    r"""unlink|\bremove\(|\brename\(|\breplace\(|rmtree|writeFile|open\([^)]*,\s*['"][wa]"""
)
_QUOTED = re.compile(r"""(?:'([^']+)'|"([^"]+)")""")
_PREFIX_WORDS = frozenset({"sudo", "command", "nohup", "time", "env", "exec", "builtin"})
_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>", "1>", "2>", "1>>", "2>>"})
_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", "|&"})
_FOLD_CASE = sys.platform in ("darwin", "win32", "cygwin")
_ROOT_TAIL = re.compile(r"""(?<!\\)[\s'";|&<>()]""")

# data-directory paths a user may edit by hand (relative to the data root)
_EDITABLE = ("config.yaml", "profile.yaml", "archive/manual.txt")
_EDITABLE_DIRS = ("daily", "logs")


@dataclass(frozen=True)
class Rule:
    prefix: str  # path relative to the data root, posix
    command: str


RULES: tuple[Rule, ...] = (
    Rule("state/feedback.jsonl", "`kallimachos feedback add` / `feedback poll`"),
    Rule("state/seen.jsonl", "`kallimachos run daily|weekly` (written by the run only)"),
    Rule("state/locks", "`kallimachos doctor --fix` (stale locks only)"),
    Rule("state/budget", "nothing: the budget ledger is written by the HTTP layer"),
    Rule("state", "the `kallimachos` command that owns it (see `kallimachos doctor`)"),
    Rule("runs", "`kallimachos select --run ID --main ...` or `kallimachos run ... --rerun`"),
    Rule("weights.json", "`kallimachos feedback apply-weights`"),
    Rule("archive/catalog.jsonl", "`kallimachos archive run|confirm|scan-existing`"),
    Rule("archive/unreachable.jsonl", "`kallimachos archive retry`"),
    Rule("archive", "`kallimachos archive ...`"),
    Rule("process/cards", "`kallimachos process card-write`"),
    Rule("process/semantic", "`kallimachos process semantic-record`"),
    Rule("process/status.jsonl", "the process commands (`process status` shows it)"),
    Rule("process/reading_queue.jsonl", "`kallimachos process queue add|sync`"),
    Rule("process/notes_index.json", "`kallimachos process notes-index rebuild`"),
    Rule("process/graph.jsonl", "`kallimachos process cite` / `process links`"),
    Rule("process", "the `kallimachos process ...` command for that step"),
    Rule("bridge", "`kallimachos bridge ...`"),
    Rule("cache", "nothing: the cache is managed by the HTTP layer"),
)

BLOCK_COMMANDS = {
    "full_reading": "`kallimachos process note-write EID --file BODY.md --existing NOTE.md`",
    "related": "`kallimachos process links`",
    "queue": "`kallimachos process queue sync`",
}


def _real(path: str | os.PathLike[str]) -> str:
    """Canonical form for comparisons: symlinks resolved; case folded where the default file
    system ignores case (APFS on macOS, NTFS on Windows), so ``.../KALLIMACHOS/state`` is the
    same place as ``.../kallimachos/state``."""
    real = os.path.normcase(os.path.realpath(os.fspath(path)))
    return real.casefold() if _FOLD_CASE else real


def protected_rule(path: str, layout: DataLayout) -> Rule | None:
    """The rule for a data-directory file that must not be edited by hand, else None."""
    root = _real(layout.root)
    target = _real(path)
    if target != root and not target.startswith(root + os.sep):
        return None
    rel = Path(os.path.relpath(target, root)).as_posix()
    if rel in _EDITABLE or rel == ".":
        return None
    if any(rel == d or rel.startswith(d + "/") for d in _EDITABLE_DIRS):
        return None
    for rule in RULES:
        if rel == rule.prefix or rel.startswith(rule.prefix + "/"):
            return rule
    return None


def managed_spans(text: str) -> list[tuple[int, int, str]]:
    """``(start, end, kind)`` of every non-exempt managed block (markers included)."""
    spans: list[tuple[int, int, str]] = []
    for m in _BEGIN.finditer(text):
        kind = m.group("kind")
        end_re = re.compile(
            rf"<!-- kallimachos:end(?: {re.escape(kind)})? -->",
        )
        e = end_re.search(text, m.end())
        if e is None or kind in EXEMPT_KINDS:
            continue
        spans.append((m.start(), e.end(), kind))
    return spans


def _read_small(path: str) -> str | None:
    try:
        p = Path(path)
        if not p.is_file() or p.stat().st_size > MAX_READ_BYTES:
            return None
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _block_reason(path: str, kinds: Iterable[str]) -> str:
    kinds = sorted(set(kinds))
    cmds = sorted({BLOCK_COMMANDS.get(k, "the `kallimachos process ...` command") for k in kinds})
    return (
        f"{Path(path).name}: the Kallimachos-managed block(s) {', '.join(kinds)} change only "
        f"through {' / '.join(cmds)}. Text outside the markers can be edited freely."
    )


def _blocks_texts(text: str) -> list[str]:
    return [text[s:e] for s, e, _k in managed_spans(text)]


def _overlaps(text: str, needle: str, spans: list[tuple[int, int, str]]) -> list[str]:
    if not needle:
        return []
    hits: list[str] = []
    start = 0
    while (i := text.find(needle, start)) != -1:
        j = i + len(needle)
        hits += [kind for s, e, kind in spans if i < e and j > s]
        start = i + 1
    return hits


def check_edit(path: str, edits: list[tuple[str, str]]) -> str | None:
    text = _read_small(path)
    for _old, new in edits:
        if MARKER in new and (text is None or new.count(MARKER) != _old.count(MARKER)):
            return _block_reason(path, ["(new marker)"])
    if text is None or MARKER not in text:
        return None
    spans = managed_spans(text)
    kinds: list[str] = []
    for old, _new in edits:
        kinds += _overlaps(text, old, spans)
    return _block_reason(path, kinds) if kinds else None


def check_write(path: str, content: str) -> str | None:
    old = _read_small(path)
    if old is None:
        if MARKER in content and managed_spans(content):
            kinds = [k for _s, _e, k in managed_spans(content)]
            return _block_reason(path, kinds)
        return None
    if MARKER not in old and MARKER not in content:
        return None
    if _blocks_texts(old) != _blocks_texts(content):
        kinds = [k for _s, _e, k in managed_spans(old)] or [
            k for _s, _e, k in managed_spans(content)
        ]
        return _block_reason(path, kinds or ["(marker)"])
    return None


_PATCH_FILE = re.compile(r"^\*\*\* (Update|Add|Delete) File: (.+?)\s*$")
_PATCH_MOVE = re.compile(r"^\*\*\* Move to: (.+?)\s*$")


def parse_patch(patch: str) -> list[tuple[str, str, list[str], list[str]]]:
    """``apply_patch`` text -> ``(action, path, removed_lines, added_lines)`` per file."""
    out: list[tuple[str, str, list[str], list[str]]] = []
    for line in patch.splitlines():
        m = _PATCH_FILE.match(line)
        if m:
            out.append((m.group(1), m.group(2), [], []))
            continue
        mv = _PATCH_MOVE.match(line)
        if mv and out:
            out.append(("Move", mv.group(1), [], []))
            continue
        if not out or line.startswith("***"):
            continue
        if line.startswith("-"):
            out[-1][2].append(line[1:])
        elif line.startswith("+"):
            out[-1][3].append(line[1:])
    return out


def check_patch(patch: str, cwd: str, layout: DataLayout) -> str | None:
    for action, raw, removed, added in parse_patch(patch):
        path = raw if os.path.isabs(raw) else os.path.join(cwd, raw)
        rule = protected_rule(path, layout)
        if rule is not None:
            return _data_reason(path, rule)
        if any(MARKER in line for line in added + removed):
            return _block_reason(path, ["(marker)"])
        text = _read_small(path)
        if text is None or MARKER not in text:
            continue
        spans = managed_spans(text)
        if action in ("Delete", "Move", "Add") and spans:
            # Add over an existing note would replace the whole file, blocks included
            return _block_reason(path, [k for _s, _e, k in spans])
        inside = "\n".join(text[s:e] for s, e, _k in spans)
        hits = [line for line in removed if line.strip() and line in inside]
        if hits:
            return _block_reason(path, [k for _s, _e, k in spans])
    return None


def _data_reason(path: str, rule: Rule) -> str:
    return (
        f"{Path(path).name} is Kallimachos state (validated, append-only or command-owned); "
        f"do not edit it with file tools. Use {rule.command}."
    )


def _tokens(command: str) -> list[str]:
    """Shell words with ``; && || | & ( ) > >>`` as separate tokens; quotes and backslash escapes
    resolved (``'Application Support'``, ``Application\\ Support``). Falls back to a whitespace
    split when the quoting is unbalanced."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return command.split()


def _simple_commands(tokens: list[str]) -> list[tuple[list[str], list[str]]]:
    """``(argv, redirect_targets)`` per simple command."""
    out: list[tuple[list[str], list[str]]] = []
    argv: list[str] = []
    targets: list[str] = []
    expect_target = False
    for tok in tokens:
        if expect_target:
            targets.append(tok)
            expect_target = False
            continue
        if tok in _SEPARATORS:
            if argv or targets:
                out.append((argv, targets))
            argv, targets = [], []
            continue
        if tok in _REDIRECTS:
            expect_target = True
            continue
        if tok.startswith((">", "&>")) and len(tok) > 1 and tok.lstrip(">&|"):
            targets.append(tok.lstrip(">&|"))
            continue
        argv.append(tok)
    if argv or targets:
        out.append((argv, targets))
    return out


def _strip_prefixes(argv: list[str]) -> list[str]:
    i = 0
    while i < len(argv) and (argv[i] in _PREFIX_WORDS or ("=" in argv[i] and i == 0)):
        i += 1
    while i < len(argv) and "=" in argv[i] and not argv[i].startswith("-") and i > 0:
        i += 1  # env VAR=value ... cmd
    return argv[i:]


def _expand(token: str, cwd: str) -> str:
    path = os.path.expandvars(os.path.expanduser(token))
    return path if os.path.isabs(path) else os.path.join(cwd, path)


def write_candidates(command: str, cwd: str) -> list[str]:
    """Paths a shell command may change: redirect targets, arguments of write verbs (``rm``,
    ``mv``, ``tee``, ``sed -i``, ``perl -pi`` ...), the destination of ``cp`` and quoted strings
    of an inline interpreter program that writes (``python -c "open(p, 'w')..."``). ``cd`` moves
    the working directory for the commands after it. Reads (``cat X``, ``X > /dev/null``) yield
    nothing."""
    out: list[str] = []
    for raw_argv, targets in _simple_commands(_tokens(command)):
        out.extend(_expand(t, cwd) for t in targets)
        argv = _strip_prefixes(raw_argv)
        if not argv:
            continue
        verb = os.path.basename(argv[0]).lower()
        if verb == "cd" and len(argv) > 1:
            cwd = _expand(argv[1], cwd)
            continue
        args = argv[1:]
        files = [a for a in args if a and not a.startswith("-")]
        if verb in _COPY_VERBS:
            if files:
                out.append(_expand(files[-1], cwd))
            continue
        if verb in _WRITE_VERBS:
            out.extend(_expand(a, cwd) for a in files)
            continue
        if verb in _INPLACE_EDITORS and any(_INPLACE_OPT.match(a) for a in args):
            out.extend(_expand(a, cwd) for a in files)
            continue
        if verb in _INTERPRETERS or verb.startswith(("python", "perl", "ruby", "node")):
            code = " ".join(a for i, a in enumerate(args) if i and args[i - 1] in _INLINE_CODE_OPTS)
            if code and _INLINE_WRITE.search(code):
                out.extend(_expand(m.group(1) or m.group(2), cwd) for m in _QUOTED.finditer(code))
                out.extend(_expand(a, cwd) for a in files if "/" in a or "\\" in a)
    return out


def _root_mentions(command: str, layout: DataLayout) -> list[str]:
    """Paths spelled with the data root inside any token (covers code strings and other quoting
    the tokenizer cannot follow)."""
    out: list[str] = []
    for root in {str(layout.root), os.path.realpath(layout.root)}:
        idx = command.find(root)
        while idx != -1:
            tail = _ROOT_TAIL.split(command[idx:], maxsplit=1)[0]
            out.append(tail.replace("\\ ", " "))
            idx = command.find(root, idx + 1)
    return out


def _managed_file_reason(path: str) -> str | None:
    text = _read_small(path)
    if text is None or MARKER not in text:
        return None
    spans = managed_spans(text)
    return _block_reason(path, [k for _s, _e, k in spans]) if spans else None


def check_shell(command: str, layout: DataLayout, cwd: str | None = None) -> str | None:
    """Deny a shell command that writes to a state file or to a note holding managed blocks.

    Only write targets are judged (see :func:`write_candidates`), so reading a state file or
    sending it to ``/dev/null`` passes. Paths are expanded (``~``, ``$HOME``), made absolute
    against ``cwd`` (following ``cd``) and resolved through symlinks before the rules apply."""
    cwd = cwd or os.getcwd()
    candidates = write_candidates(command, cwd)
    if not candidates:
        return None
    candidates.extend(_root_mentions(command, layout))
    for cand in candidates:
        rule = protected_rule(cand, layout)
        if rule is not None:
            return _data_reason(cand, rule)
    for cand in candidates:
        reason = _managed_file_reason(cand)
        if reason is not None:
            return reason
    return None


def decide(payload: Mapping[str, Any], layout: DataLayout) -> str | None:
    """Reason to deny the tool call, or None to let it through (fail-open)."""
    tool = str(payload.get("tool_name") or "")
    tin = payload.get("tool_input")
    if not isinstance(tin, Mapping):
        return None
    cwd = str(payload.get("cwd") or os.getcwd())
    if tool in ("Bash", "PowerShell"):
        cmd = tin.get("command")
        return check_shell(cmd, layout, cwd) if isinstance(cmd, str) else None
    if tool == "apply_patch":
        patch = tin.get("command") or tin.get("patch") or tin.get("input")
        if isinstance(patch, list):
            patch = "\n".join(str(p) for p in patch)
        return check_patch(patch, cwd, layout) if isinstance(patch, str) else None
    path = tin.get("file_path") or tin.get("notebook_path")
    if not isinstance(path, str) or not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    rule = protected_rule(path, layout)
    if rule is not None:
        return _data_reason(path, rule)
    if tool == "Write":
        content = tin.get("content")
        return check_write(path, content) if isinstance(content, str) else None
    if tool == "Edit":
        old, new = tin.get("old_string"), tin.get("new_string")
        if isinstance(old, str) and isinstance(new, str):
            return check_edit(path, [(old, new)])
        return None
    if tool == "MultiEdit":
        edits = tin.get("edits")
        if not isinstance(edits, list):
            return None
        pairs = [
            (str(e.get("old_string", "")), str(e.get("new_string", "")))
            for e in edits
            if isinstance(e, Mapping)
        ]
        return check_edit(path, pairs)
    return None


def deny_output(reason: str) -> dict[str, Any]:
    """The PreToolUse deny shape documented by both Claude Code and Codex."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"Kallimachos guard: {reason}",
        }
    }
