"""schemas/*.json are generated from the dataclasses and the examples validate."""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import typing
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

import kallimachos.config.schema
import kallimachos.profile.schema  # noqa: F401  registers profile.schema.json
from kallimachos.config.schema import Config
from kallimachos.interfaces import SCHEMA_REGISTRY, build, dump, json_schema
from kallimachos.profile.schema import Profile

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"
REPO = SCHEMAS_DIR.parent


def _load_gen_tool() -> Any:
    spec = importlib.util.spec_from_file_location("gen_schemas", REPO / "tools" / "gen_schemas.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema(name: str) -> dict[str, Any]:
    data = json.loads((SCHEMAS_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_registry_covers_every_schema_file_and_vice_versa() -> None:
    on_disk = {p.name for p in SCHEMAS_DIR.glob("*.schema.json")}
    assert on_disk == set(SCHEMA_REGISTRY)
    required = {
        "config.schema.json",
        "profile.schema.json",
        "run.schema.json",
        "selection.schema.json",
        "feedback.schema.json",
        "seen.schema.json",
        "weights.schema.json",
        "catalog.schema.json",
        "card.schema.json",
        "semantic_result.schema.json",
        "note_frontmatter.schema.json",
        "cluster_frontmatter.schema.json",
        "reading_queue.schema.json",
        "status.schema.json",
    }
    assert required <= on_disk


@pytest.mark.parametrize("name", sorted(SCHEMA_REGISTRY))
def test_schema_file_matches_dataclass(name: str) -> None:
    cls, schema_id = SCHEMA_REGISTRY[name]
    assert _schema(name) == json_schema(cls, schema_id=schema_id), (
        "run: python tools/gen_schemas.py"
    )


def test_gen_tool_check_mode_passes() -> None:
    assert _load_gen_tool().main(["--check"]) == 0


@pytest.mark.parametrize("name", sorted(SCHEMA_REGISTRY))
def test_schema_is_valid_draft_2020_12(name: str) -> None:
    jsonschema.Draft202012Validator.check_schema(_schema(name))


def _walk(cls: type[Any], schema: dict[str, Any], defs: dict[str, Any], path: str) -> None:
    props = schema["properties"]
    hints = typing.get_type_hints(cls)
    names = {f.metadata.get("alias", f.name) for f in dataclasses.fields(cls) if f.init}
    assert set(props) == names, path
    assert schema.get("additionalProperties") is False, path
    for f in dataclasses.fields(cls):
        tp = hints[f.name]
        inner = tp
        if typing.get_origin(tp) in (typing.Union, type(None) | int):
            args = [a for a in typing.get_args(tp) if a is not type(None)]
            inner = args[0] if len(args) == 1 else None
        if isinstance(inner, type) and dataclasses.is_dataclass(inner):
            ref = props[f.metadata.get("alias", f.name)]
            ref = ref["anyOf"][0] if "anyOf" in ref else ref
            assert ref["$ref"] == f"#/$defs/{inner.__name__}", f"{path}.{f.name}"
            _walk(inner, defs[inner.__name__], defs, f"{path}.{f.name}")


@pytest.mark.parametrize("name", sorted(SCHEMA_REGISTRY))
def test_property_names_match_dataclass_fields_recursively(name: str) -> None:
    cls, _ = SCHEMA_REGISTRY[name]
    schema = _schema(name)
    _walk(cls, schema, schema.get("$defs", {}), cls.__name__)


def test_config_example_builds_and_validates() -> None:
    raw = yaml.safe_load((REPO / "config.example.yaml").read_text(encoding="utf-8"))
    cfg = build(Config, raw)
    jsonschema.Draft202012Validator(_schema("config.schema.json")).validate(dump(cfg))
    # the example must spell out every top-level section so users see the whole surface
    assert set(raw) == {f.name for f in dataclasses.fields(Config)}
    assert cfg.privacy.send_only_strong_ids is True
    assert cfg.notify.email.confirmed_at is None


def test_profile_example_builds_and_validates() -> None:
    raw = yaml.safe_load(
        (REPO / "profiles" / "example-number-theory.yaml").read_text(encoding="utf-8")
    )
    profile = build(Profile, raw)
    jsonschema.Draft202012Validator(_schema("profile.schema.json")).validate(dump(profile))
    assert profile.status == "draft" and not profile.is_approved
    assert [c.id for c in profile.channels] == ["A", "B", "C"]
    assert profile.channel_by_id("B") is not None
    assert profile.channel_by_id("B").rules[0].stage == "drop"  # type: ignore[union-attr]
    assert "Example" in profile.authors.always_show[0].name  # fictional data only


def test_example_profile_rejects_dead_key() -> None:
    raw = yaml.safe_load(
        (REPO / "profiles" / "example-number-theory.yaml").read_text(encoding="utf-8")
    )
    raw["channels"][0]["colour"] = "red"
    with pytest.raises(Exception, match="unknown key"):
        build(Profile, raw)
