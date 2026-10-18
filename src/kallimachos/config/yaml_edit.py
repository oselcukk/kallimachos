"""Targeted edits of a YAML file's text that keep the user's comments and layout.

``kallimachos`` rewrites ``config.yaml`` / ``profile.yaml`` wholesale only when it creates them.
Later single-value changes (``notify confirm-email`` sets ``notify.email.confirmed_at``,
``profile approve`` sets ``status`` and ``approved_at``) go through :func:`set_scalars`, which
replaces or inserts one ``key: value`` line inside block mappings and leaves everything else
byte for byte. Anything it cannot place safely (flow mappings, block scalars, anchors) returns
``None`` and the caller falls back to a full rewrite after taking a backup.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

import yaml

_KEY_LINE = re.compile(
    r"^(?P<indent> *)(?P<key>[^\s#\-'\"{}\[\]][^:#]*?|'[^']*'|\"[^\"]*\"):(?P<rest>.*)$"
)
_REST = re.compile(r"""^(?P<gap>\s*)(?P<value>'[^']*'|"[^"]*"|[^#]*?)(?P<comment>\s*#.*)?$""")


def scalar_text(value: Any) -> str:
    """The YAML spelling of a scalar (``null``, ``true``, ``'2026-10-18T10:00:00+00:00'``)."""
    text = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True).strip()
    if text.endswith("\n..."):
        text = text[: -len("\n...")]
    return text.strip()


def _unquote(key: str) -> str:
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "'\"":
        return key[1:-1]
    return key.strip()


def _is_blank(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _region_end(lines: Sequence[str], start: int, indent: int) -> int:
    """Index after the last line belonging to the value of the key at ``start`` (``indent``)."""
    i = start + 1
    last = start + 1
    while i < len(lines):
        line = lines[i]
        if _is_blank(line):
            i += 1
            continue
        if len(line) - len(line.lstrip(" ")) <= indent:
            break
        i += 1
        last = i
    return last


def _find_key(lines: Sequence[str], key: str, start: int, end: int, indent: int) -> int | None:
    for i in range(start, end):
        m = _KEY_LINE.match(lines[i].rstrip("\n"))
        if m and len(m.group("indent")) == indent and _unquote(m.group("key")) == key:
            return i
    return None


def _child_indent(lines: Sequence[str], start: int, end: int) -> int | None:
    for i in range(start, end):
        if not _is_blank(lines[i]):
            return len(lines[i]) - len(lines[i].lstrip(" "))
    return None


def _set_one(lines: list[str], keys: Sequence[str], value: Any) -> list[str] | None:
    start, end, indent = 0, len(lines), 0
    for depth, key in enumerate(keys):
        at = _find_key(lines, key, start, end, indent)
        last = depth == len(keys) - 1
        if at is None:
            if not last:
                return None  # a missing parent mapping: let the caller rewrite the file
            new_line = f"{' ' * indent}{key}: {scalar_text(value)}\n"
            insert_at = end
            while insert_at > start and _is_blank(lines[insert_at - 1]):
                insert_at -= 1
            if insert_at and not lines[insert_at - 1].endswith("\n"):
                lines[insert_at - 1] += "\n"
            return [*lines[:insert_at], new_line, *lines[insert_at:]]
        m = _KEY_LINE.match(lines[at].rstrip("\n"))
        assert m is not None
        rest = m.group("rest")
        region_end = _region_end(lines, at, indent)
        if last:
            parsed = _REST.match(rest)
            if parsed is None or (
                region_end > at + 1 and not _only_blank(lines, at + 1, region_end)
            ):
                return None  # the value is a nested block, not a scalar
            scalar = parsed.group("value").strip()
            if scalar in ("|", ">", "{", "[") or scalar.startswith(("&", "*", "!", "|", ">")):
                return None
            comment = parsed.group("comment") or ""
            eol = "\n" if lines[at].endswith("\n") else ""
            lines[at] = f"{m.group('indent')}{m.group('key')}: {scalar_text(value)}{comment}{eol}"
            return lines
        if rest.strip() and not rest.strip().startswith("#"):
            return None  # inline value (`email: {}` / `email: null`): not a block mapping
        child = _child_indent(lines, at + 1, region_end)
        if child is None:
            child = indent + 2
        if lines[region_end - 1 : region_end] and not _is_blank(lines[region_end - 1]):
            pass
        start, end, indent = at + 1, region_end, child
    return lines


def _only_blank(lines: Sequence[str], start: int, end: int) -> bool:
    return all(_is_blank(lines[i]) for i in range(start, end))


def set_scalars(text: str, changes: Mapping[Sequence[str], Any]) -> str | None:
    """Return ``text`` with each dotted key path set to its scalar value, or ``None`` when the
    edit cannot be done safely. The result is re-parsed and checked value by value."""
    lines = text.splitlines(keepends=True)
    for keys, value in changes.items():
        updated = _set_one(lines, list(keys), value)
        if updated is None:
            return None
        lines = updated
    result = "".join(lines)
    try:
        data = yaml.safe_load(result)
    except yaml.YAMLError:
        return None
    for keys, value in changes.items():
        node: Any = data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
        if node != yaml.safe_load(scalar_text(value)):
            return None
    return result
