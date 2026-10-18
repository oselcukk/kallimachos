"""``reset-citekeys``: clear ``citationKey`` so Better BibTeX regenerates.

Selection: ``--keys K1,K2`` (item keys) or ``--filter FIELD=TEXT`` (case-insensitive substring on
a ``data`` field, e.g. ``title=lattice``; ``extra=eid:`` selects Kallimachos items). Items whose
``citationKey`` is already empty are left alone, so the command is idempotent.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from kallimachos.interfaces import UsageError
from kallimachos.process.zotero import ZoteroClient, item_data

CITEKEY_FIELD = "citationKey"


def parse_filter(expr: str) -> tuple[str, str]:
    if "=" not in expr:
        raise UsageError("--filter expects FIELD=TEXT (e.g. title=lattice)")
    fld, text = expr.split("=", 1)
    fld = fld.strip()
    if not fld:
        raise UsageError("--filter expects FIELD=TEXT (e.g. title=lattice)")
    return fld, text.strip().lower()


def select_items(
    items: Sequence[dict[str, Any]], *, keys: Sequence[str] | None, item_filter: str | None
) -> list[dict[str, Any]]:
    if not keys and not item_filter:
        raise UsageError("reset-citekeys needs --keys or --filter (refusing to touch everything)")
    wanted = {k.strip() for k in (keys or []) if k.strip()}
    fld, text = parse_filter(item_filter) if item_filter else ("", "")
    out: list[dict[str, Any]] = []
    for item in items:
        data = item_data(item)
        if data.get("itemType") in ("attachment", "note", "annotation"):
            continue
        key = str(data.get("key") or item.get("key") or "")
        if wanted and key not in wanted:
            continue
        if fld and text not in str(data.get(fld, "")).lower():
            continue
        out.append(item)
    return out


def reset_citekeys(
    client: ZoteroClient, *, keys: Sequence[str] | None, item_filter: str | None, dry_run: bool
) -> int:
    """Blank ``citationKey`` on the selected items; return how many were changed."""
    selected = select_items(client.items(), keys=keys, item_filter=item_filter)
    changed: list[dict[str, Any]] = []
    for item in selected:
        data = item_data(item)
        if data.get(CITEKEY_FIELD):
            payload = dict(data)
            payload[CITEKEY_FIELD] = ""
            payload.setdefault("key", item.get("key"))
            payload.setdefault("version", item.get("version"))
            changed.append(payload)
    client.update_items(changed, dry_run=dry_run)
    return len(changed)
