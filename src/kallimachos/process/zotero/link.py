"""``link``: Zotero item <-> note via eid; writes ``zotero``/``citekey`` frontmatter.

Runs at chunk close only; idempotent. The matching key is the eid: ``extra`` ``eid: <eid>`` on
the Zotero side (``ris`` writes it as ``N1 eid:``; Zotero imports ``N1`` into ``extra``) and
``notes_index.json`` on the note side. Only the two frontmatter fields ``zotero`` and ``citekey``
are touched, through ``process.note.update_frontmatter_fields``; the Zotero side
gets one marked child note with the ``obsidian://`` (vault) or ``file://`` (plain folder) link.
"""

from __future__ import annotations

import html
import urllib.parse
from pathlib import Path
from typing import Any

from kallimachos.interfaces import UsageError
from kallimachos.paths import DataLayout
from kallimachos.process.zotero import NOTE_MARK, ZoteroClient, extra_value, item_data
from kallimachos.state import store

CITEKEY_FIELD = "citationKey"


def obsidian_uri(vault_name: str, relative_path: str) -> str:
    """``obsidian://open?vault=<name>&file=<path>`` (path without ``.md``, URL-encoded)."""
    rel = relative_path.replace("\\", "/")
    if rel.endswith(".md"):
        rel = rel[:-3]
    return (
        "obsidian://open?vault="
        + urllib.parse.quote(vault_name, safe="")
        + "&file="
        + urllib.parse.quote(rel, safe="/")
    )


def zotero_select_uri(
    key: str, *, library_type: str = "user", library_id: str | None = None
) -> str:
    if library_type == "group" and library_id:
        return f"zotero://select/groups/{library_id}/items/{key}"
    return f"zotero://select/library/items/{key}"


def note_html(target_uri: str, relative_path: str) -> str:
    return (
        f"<p><b>{NOTE_MARK}:</b> "
        f'<a href="{html.escape(target_uri, quote=True)}">{html.escape(relative_path)}</a></p>'
    )


def load_notes_index(layout: DataLayout) -> dict[str, dict[str, Any]]:
    data = store.read_json(layout.notes_index_file, default={})
    if not isinstance(data, dict):
        raise UsageError("notes_index.json must be a JSON object {eid: {path, ...}}")
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def items_by_eid(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        data = item_data(item)
        if data.get("itemType") in ("attachment", "note", "annotation"):
            continue
        eid = extra_value(data, "eid")
        if eid and eid not in out:
            out[eid] = item
    return out


def child_notes(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        data = item_data(item)
        if data.get("itemType") == "note" and data.get("parentItem"):
            out.setdefault(str(data["parentItem"]), []).append(item)
    return out


def _update_frontmatter(path: Path, fields: dict[str, str], *, dry_run: bool) -> bool:
    """Write ``zotero``/``citekey`` into the note's frontmatter; True when the file changed."""
    from kallimachos.process import note as note_mod  # resolved at call time

    text = path.read_text(encoding="utf-8")
    new_text = note_mod.update_frontmatter_fields(text, fields)
    if new_text == text:
        return False
    if not dry_run:
        store.write_text_atomic(path, new_text)
    return True


def link_notes(
    client: ZoteroClient,
    layout: DataLayout,
    *,
    vault_name: str | None,
    link_style: str,
    dry_run: bool,
    notes_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Link every indexed note to its Zotero item by eid; return one report row per eid.

    Rows: ``{eid, status: linked|unchanged|no_item|note_missing, key, citekey, frontmatter,
    zotero_note}``. ``link_style`` ``wikilink`` (Obsidian) uses ``obsidian://`` when ``vault_name``
    is given; otherwise ``file://`` under ``notes_root``.
    """
    index = load_notes_index(layout)
    items = client.items()
    by_eid = items_by_eid(items)
    notes = child_notes(items)
    report: list[dict[str, Any]] = []
    for eid, ref in sorted(index.items()):
        rel = str(ref.get("path", ""))
        row: dict[str, Any] = {"eid": eid, "path": rel, "status": "unchanged"}
        item = by_eid.get(eid)
        if item is None:
            row["status"] = "no_item"
            report.append(row)
            continue
        data = item_data(item)
        key = str(data.get("key") or item.get("key"))
        row["key"] = key
        citekey = str(data.get(CITEKEY_FIELD) or "")
        row["citekey"] = citekey or None
        fields = {
            "zotero": zotero_select_uri(
                key, library_type=client.cfg.library_type, library_id=str(client.cfg.user_id)
            )
        }
        if citekey:
            fields["citekey"] = citekey
        note_path = Path(notes_root, rel) if notes_root else Path(rel)
        if not note_path.is_file():
            row["status"] = "note_missing"
            report.append(row)
            continue
        changed = _update_frontmatter(note_path, fields, dry_run=dry_run)
        row["frontmatter"] = "updated" if changed else "unchanged"

        if vault_name and link_style == "wikilink":
            target = obsidian_uri(vault_name, rel)
        else:
            target = note_path.resolve().as_uri()
        wanted = note_html(target, rel)
        existing = next(
            (n for n in notes.get(key, []) if NOTE_MARK in str(item_data(n).get("note", ""))),
            None,
        )
        if existing is None:
            client.create_note(key, wanted, dry_run=dry_run)
            row["zotero_note"] = "created"
        elif item_data(existing).get("note") != wanted:
            payload = dict(item_data(existing))
            payload["note"] = wanted
            payload.setdefault("key", existing.get("key"))
            payload.setdefault("version", existing.get("version"))
            client.update_items([payload], dry_run=dry_run)
            row["zotero_note"] = "updated"
        else:
            row["zotero_note"] = "unchanged"
        if changed or row["zotero_note"] != "unchanged":
            row["status"] = "linked"
        report.append(row)
    return report
