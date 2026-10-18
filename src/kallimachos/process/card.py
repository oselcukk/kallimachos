"""Card delivery (``process card-write``) and
TR-v4 migration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jsonschema

from kallimachos.interfaces import (
    SCHEMA_REGISTRY,
    Card,
    ItemType,
    SchemaViolation,
    WorkState,
    build,
    dump,
    json_schema,
)
from kallimachos.paths import DataLayout
from kallimachos.process import status as status_mod
from kallimachos.state import store

CARD_SCHEMA = json_schema(SCHEMA_REGISTRY["card.schema.json"][0])
QUOTE_MIN = 40
QUOTE_MAX = 400

OPEN_TYPES: frozenset[ItemType] = frozenset(
    {ItemType.OPEN_PROBLEM, ItemType.QUESTION, ItemType.CONJECTURE, ItemType.RESEARCH_DIRECTION}
)
"""Items that carry a ``status`` (open, solved here, ...)."""

SOLUTION_KINDS: frozenset[str] = frozenset(
    {
        "problem",
        "conjecture",
        "question",
        "unproved_claim",
        "generalization",
        "verification",
        "counterexample",
    }
)


def item_rule_errors(card: Card) -> list[str]:
    """Item rules beyond the JSON Schema.

    * open-type items need ``status``; ``solved_elsewhere`` needs non-empty ``solution_cites``;
    * ``claimed_solution`` needs ``solution_kind`` (known value), ``target`` and ``cites``;
    * ``quoted_from_other AND type == definition`` -> ``importance`` must be ``supporting``;
    * every ``cites``/``solution_cites`` key must exist in ``references``;
    * quote length 40-400 (also enforced by the schema; repeated here with a readable message).
    """
    errors: list[str] = []
    ref_keys = {ref.key for ref in card.references}
    for i, item in enumerate(card.items):
        where = f"items[{i}] ({item.label}, p. {item.page})"
        if not QUOTE_MIN <= len(item.quote) <= QUOTE_MAX:
            errors.append(
                f"{where}: quote length {len(item.quote)} outside {QUOTE_MIN}-{QUOTE_MAX}"
            )
        if item.type in OPEN_TYPES:
            if item.status is None:
                errors.append(f"{where}: open item without status")
            elif item.status.value == "solved_elsewhere" and not item.solution_cites:
                errors.append(f"{where}: solved_elsewhere without solution_cites")
        if item.type is ItemType.CLAIMED_SOLUTION:
            if item.solution_kind not in SOLUTION_KINDS:
                errors.append(
                    f"{where}: claimed_solution needs solution_kind in {sorted(SOLUTION_KINDS)}"
                )
            if not item.target:
                errors.append(f"{where}: claimed_solution without target")
            if not item.cites:
                errors.append(f"{where}: claimed_solution without cites")
        if (
            item.quoted_from_other
            and item.type is ItemType.DEFINITION
            and item.importance != "supporting"
        ):
            errors.append(
                f"{where}: quoted_from_other definition must have importance 'supporting'"
            )
        for key in [*item.cites, *(item.solution_cites or [])]:
            if key not in ref_keys:
                errors.append(f"{where}: cites unknown reference key {key!r}")
    return errors


def validate_card(data: dict[str, Any]) -> Card:
    """Strict build + ``card.schema.json`` + item rules (quote length, importance AND-rule)."""
    card = build(Card, data, path="card")
    try:
        jsonschema.validate(dump(card), CARD_SCHEMA)
    except jsonschema.ValidationError as exc:
        path = ".".join(str(p) for p in exc.absolute_path)
        raise SchemaViolation(exc.message, path) from None
    errors = item_rule_errors(card)
    if errors:
        raise SchemaViolation("; ".join(errors), "card")
    return card


def card_path(layout: DataLayout, eid: str) -> Path:
    return layout.cards_dir / f"{eid}.json"


def write_card(layout: DataLayout, card: Card, *, by: str = "process card-write") -> Path:
    """Write ``cards/<eid>.json`` atomically and record ``card_written``."""
    path = card_path(layout, card.eid)
    status_mod.check_transition(layout, card.eid, WorkState.CARD_WRITTEN)
    store.write_json_atomic(path, dump(card), schema=CARD_SCHEMA)
    status_mod.record_state(layout, card.eid, WorkState.CARD_WRITTEN, by=by)
    return path


def load_card(layout: DataLayout, eid: str) -> Card | None:
    data = store.read_json(card_path(layout, eid))
    if data is None:
        return None
    return build(Card, data, path="card")


def load_all_cards(layout: DataLayout) -> list[Card]:
    cards: list[Card] = []
    if not layout.cards_dir.is_dir():
        return cards
    for path in sorted(layout.cards_dir.glob("*.json")):
        data = store.read_json(path)
        if isinstance(data, dict):
            cards.append(build(Card, data, path=path.name))
    return cards


# --- TR-v4 migration ------------------------------------------------------------

_ROOT_KEYS = {
    "eid": "eid",
    "yol": "path",
    "sayfa_sayisi": "pages",
    "kunye": "citation",
    "ozet_tr": "summary",
    "ogeler": "items",
    "kaynakca": "references",
    "kavramlar": "concepts",
    "isaretler": "flags",
    "yanlis_alarm": "false_alarms",
    "schema": "schema",
}
_CITATION_KEYS = {
    "yazarlar": "authors",
    "yil": "year",
    "yil_katalog": "year_catalog",
    "yil_pdf": "year_pdf",
    "yil_not": "year_note",
    "baslik": "title",
    "yayin": "venue",
    "doi": "doi",
    "arxiv": "arxiv",
    "sayfa_araligi": "page_range",
}
_ITEM_KEYS = {
    "tur": "type",
    "numara": "number",
    "baslik": "label",
    "sayfa": "page",
    "basili_sayfa": "printed_page",
    "alinti": "quote",
    "ifade_tr": "statement",
    "kavramlar": "concepts",
    "atif": "cites",
    "onem": "importance",
    "atif_edilen": "quoted_from_other",
    "durum": "status",
    "cozum_atif": "solution_cites",
    "somutluk": "concreteness",
    "durum_not": "status_note",
    "cozum_turu": "solution_kind",
    "hedef": "target",
}
_REFERENCE_KEYS = {"anahtar": "key", "metin": "text", "doi": "doi", "arxiv": "arxiv"}
_FLAG_KEYS = {
    "zorlandim": "struggled",
    "potansiyel": "potential",
    "gerekce": "rationale",
    "ocr_zayif": "ocr_weak",
    "okunmayan_sayfalar": "unreadable_pages",
    "formul_guvenilmez": "formulas_unreliable",
}
_FALSE_ALARM_KEYS = {"sayfa": "page", "ifade": "phrase", "neden": "reason"}

_VALUE_MAPS: dict[str, dict[str, str]] = {
    "importance": {"ana": "main", "yardimci": "supporting"},
    "status": {
        "acik": "open",
        "cozuldu_burada": "solved_here",
        "kismen_cozuldu_burada": "partly_solved_here",
        "cozuldu_baska_eserde": "solved_elsewhere",
    },
    "concreteness": {"somut": "concrete", "ucu_acik": "open_ended"},
    "solution_kind": {
        "ispatsiz_iddia": "unproved_claim",
        "genelleme": "generalization",
        "dogrulama": "verification",
        "karsi_ornek": "counterexample",
    },
    "potential": {"dusuk": "low", "orta": "medium", "yuksek": "high"},
}


def _rename(data: Any, table: dict[str, str], *, drop: frozenset[str] = frozenset()) -> Any:
    if not isinstance(data, dict):
        return data
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key in drop:
            continue
        new_key = table.get(str(key), str(key))
        if new_key in _VALUE_MAPS and isinstance(value, str):
            value = _VALUE_MAPS[new_key].get(value, value)
        out[new_key] = value
    return out


def migrate_card_tr_v4(data: dict[str, Any]) -> dict[str, Any]:
    """Rename Turkish v4 keys to the English card/4 keys (fixed key table below).

    Values of ``status``/``importance``/``concreteness``/``solution_kind``/``potential`` are
    translated too. Keys that are not in the table are kept unchanged, so ``validate_card``
    reports anything the migration did not understand. ``kunye.sayfa`` (removed in v2) is dropped.
    """
    out: dict[str, Any] = _rename(data, _ROOT_KEYS)
    out["schema"] = "kallimachos.card/4"
    if "citation" in out:
        out["citation"] = _rename(out["citation"], _CITATION_KEYS, drop=frozenset({"sayfa"}))
    if isinstance(out.get("items"), list):
        out["items"] = [_rename(item, _ITEM_KEYS) for item in out["items"]]
    if isinstance(out.get("references"), list):
        out["references"] = [_rename(ref, _REFERENCE_KEYS) for ref in out["references"]]
    if "flags" in out:
        out["flags"] = _rename(out["flags"], _FLAG_KEYS)
    if isinstance(out.get("false_alarms"), list):
        out["false_alarms"] = [_rename(fa, _FALSE_ALARM_KEYS) for fa in out["false_alarms"]]
    return out
