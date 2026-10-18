"""Archive JSONL rows and their persistence through ``state.store``.

Every row class lives in ``kallimachos.interfaces`` (``CatalogEntry``, ``UnreachableEntry``,
``ProcessedEntry``, ``ExistingEntry``, ``ArchiveLogLine``) and is registered in
``SCHEMA_REGISTRY``; the published files under ``schemas/`` come from ``tools/gen_schemas.py``.

Full rewrites go through :func:`write_rows`: every row is validated, then the whole file is
written with ``state.store.write_jsonl_atomic`` (tmp + ``os.replace``, NFC).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, TypeVar

import jsonschema

from kallimachos.interfaces import (
    SCHEMA_REGISTRY,
    ArchiveLogLine,
    ExistingEntry,
    ProcessedEntry,
    SchemaViolation,
    build,
    dump,
    json_schema,
)
from kallimachos.state import store

__all__ = [
    "ArchiveLogLine",
    "ExistingEntry",
    "ProcessedEntry",
    "append_row",
    "read_rows",
    "schema_for",
    "validate",
    "write_rows",
]

T = TypeVar("T")


_SCHEMAS: dict[type[Any], dict[str, Any]] = {}


def schema_for(cls: type[Any]) -> dict[str, Any]:
    """JSON Schema of a registered row class (same generator as ``schemas/``)."""
    if cls not in _SCHEMAS:
        schema_id = next((sid for c, sid in SCHEMA_REGISTRY.values() if c is cls), None)
        _SCHEMAS[cls] = json_schema(cls, schema_id=schema_id) if schema_id else json_schema(cls)
    return _SCHEMAS[cls]


def validate(row: Any) -> dict[str, Any]:
    data: dict[str, Any] = dump(row)
    try:
        jsonschema.validate(data, schema_for(type(row)))
    except jsonschema.ValidationError as exc:
        raise SchemaViolation(f"{type(row).__name__}: {exc.message}") from exc
    return data


def read_rows(path: Path, cls: type[T]) -> list[T]:
    return [build(cls, obj) for obj in store.read_jsonl(path)]


def write_rows(path: Path, rows: Sequence[Any]) -> None:
    """Validated full rewrite through ``state.store.write_jsonl_atomic``."""
    store.write_jsonl_atomic(path, (validate(r) for r in rows))


def append_row(path: Path, row: Any) -> None:
    store.append_jsonl(path, dump(row), schema=schema_for(type(row)))
