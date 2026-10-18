"""Semantic verification record (``process semantic-record``).

``note-write`` refuses (exit 5) without a ``pass`` record for the eid.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jsonschema

from kallimachos.interfaces import (
    SCHEMA_REGISTRY,
    SchemaViolation,
    SemanticResult,
    WorkState,
    build,
    dump,
    json_schema,
)
from kallimachos.paths import DataLayout
from kallimachos.process import status as status_mod
from kallimachos.state import store

SEMANTIC_SCHEMA = json_schema(SCHEMA_REGISTRY["semantic_result.schema.json"][0])


def validate_semantic(data: dict[str, Any], *, eid: str) -> SemanticResult:
    """Strict build + schema + consistency: the record must be for ``eid``; a ``pass`` cannot
    carry a ``major`` mismatch; ``verifier_model`` is required (a separate verifier agent)."""
    payload = dict(data)
    payload.setdefault("eid", eid)
    result = build(SemanticResult, payload, path="semantic_result")
    try:
        jsonschema.validate(dump(result), SEMANTIC_SCHEMA)
    except jsonschema.ValidationError as exc:
        path = ".".join(str(p) for p in exc.absolute_path)
        raise SchemaViolation(exc.message, path) from None
    if result.eid != eid:
        raise SchemaViolation(f"record is for {result.eid}, command was given {eid}", "eid")
    if not result.verifier_model.strip():
        raise SchemaViolation("verifier_model is required", "verifier_model")
    if result.result == "pass" and any(m.severity == "major" for m in result.mismatches):
        raise SchemaViolation("a pass record cannot contain a major mismatch", "mismatches")
    return result


def semantic_path(layout: DataLayout, eid: str) -> Path:
    return layout.semantic_dir / f"{eid}.json"


def record_semantic(
    layout: DataLayout, result: SemanticResult, *, by: str = "process semantic-record"
) -> Path:
    """Write ``semantic/<eid>.json`` and record ``semantic_verified`` / ``semantic_failed``."""
    path = semantic_path(layout, result.eid)
    state = WorkState.SEMANTIC_VERIFIED if result.result == "pass" else WorkState.SEMANTIC_FAILED
    status_mod.check_transition(layout, result.eid, state)
    store.write_json_atomic(path, dump(result), schema=SEMANTIC_SCHEMA)
    status_mod.record_state(layout, result.eid, state, by=by)
    return path


def latest_semantic(layout: DataLayout, eid: str) -> SemanticResult | None:
    data = store.read_json(semantic_path(layout, eid))
    if data is None:
        return None
    return build(SemanticResult, data, path="semantic_result")
