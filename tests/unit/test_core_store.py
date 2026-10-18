"""state.store: atomic writes, NFC, schema validation, JSONL reading."""

from __future__ import annotations

import datetime as dt
import json
import os
import unicodedata
from pathlib import Path

import pytest

from kallimachos.interfaces import FeedbackEntry, FeedbackTag, SchemaViolation, SeenEntry, dump
from kallimachos.state import store

DECOMPOSED = unicodedata.normalize("NFD", "Erdős–Gödel çağ")
COMPOSED = unicodedata.normalize("NFC", DECOMPOSED)


def test_write_json_atomic_uses_tmp_and_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []
    real = os.replace

    def spy(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        calls.append((str(src), str(dst)))
        real(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    target = tmp_path / "sub" / "data.json"
    store.write_json_atomic(target, {"a": 1})
    assert len(calls) == 1
    src, dst = calls[0]
    assert dst == str(target)
    assert Path(src).parent == target.parent and Path(src).name.endswith(".tmp")
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert [p.name for p in target.parent.iterdir()] == ["data.json"]


def test_failed_write_leaves_old_file_and_no_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "w" / "data.json"
    store.write_json_atomic(target, {"v": 1})

    def boom(*_a: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.write_json_atomic(target, {"v": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 1}
    assert [p.name for p in target.parent.iterdir()] == ["data.json"]


def test_nfc_on_write_everywhere(tmp_path: Path) -> None:
    store.write_json_atomic(tmp_path / "a.json", {DECOMPOSED: [DECOMPOSED, {"k": DECOMPOSED}]})
    text = (tmp_path / "a.json").read_text(encoding="utf-8")
    assert DECOMPOSED not in text and COMPOSED in text
    assert "\\u" not in text  # ensure_ascii=False
    store.append_jsonl(tmp_path / "b.jsonl", {"title": DECOMPOSED})
    assert next(store.read_jsonl(tmp_path / "b.jsonl"))["title"] == COMPOSED
    store.write_text_atomic(tmp_path / "c.md", f"# {DECOMPOSED}\n")
    assert (tmp_path / "c.md").read_text(encoding="utf-8") == f"# {COMPOSED}\n"


def test_normalize_for_storage_handles_records() -> None:
    entry = FeedbackEntry(
        at=dt.datetime(2026, 10, 18, 9, 0, tzinfo=dt.UTC),
        run_id="2026-10-18-daily",
        item_no="3",
        candidate_id="arXiv:2510.00001",
        tag=FeedbackTag.READ,
        note=DECOMPOSED,
    )
    plain = store.normalize_for_storage(entry)
    assert plain["tag"] == "read" and plain["note"] == COMPOSED
    assert plain["at"].startswith("2026-10-18T09:00")


def test_schema_validation_on_write(tmp_path: Path) -> None:
    schema = store.schema_for("seen.schema.json")
    good = SeenEntry(
        keys=["arxiv:2510.00001"], first_run="2026-10-18-daily", last_run="2026-10-18-daily"
    )
    store.append_jsonl(tmp_path / "seen.jsonl", dump(good), schema=schema)
    bad = dump(good) | {"first_run": "yesterday"}
    with pytest.raises(SchemaViolation) as exc:
        store.append_jsonl(tmp_path / "seen.jsonl", bad, schema=schema)
    assert exc.value.path == "first_run"
    with pytest.raises(SchemaViolation):
        store.write_json_atomic(tmp_path / "x.json", {"unknown": 1}, schema=schema)
    assert len(list(store.read_jsonl(tmp_path / "seen.jsonl"))) == 1
    assert not (tmp_path / "x.json").exists()


def test_schema_for_config_and_unknown() -> None:
    assert store.schema_for("config.schema.json")["title"] == "Config"
    with pytest.raises(SchemaViolation):
        store.schema_for("nope.schema.json")


def test_read_jsonl_missing_blank_and_corrupt(tmp_path: Path) -> None:
    assert list(store.read_jsonl(tmp_path / "missing.jsonl")) == []
    path = tmp_path / "f.jsonl"
    path.write_text('{"a": 1}\n\n{"a": 2}\n{broken\n', encoding="utf-8")
    it = store.read_jsonl(path)
    assert next(it) == {"a": 1} and next(it) == {"a": 2}
    with pytest.raises(SchemaViolation) as exc:
        next(it)
    assert "f.jsonl:4" in str(exc.value)
    path.write_text("[1, 2]\n", encoding="utf-8")
    with pytest.raises(SchemaViolation):
        list(store.read_jsonl(path))


def test_append_jsonl_requires_object(tmp_path: Path) -> None:
    with pytest.raises(SchemaViolation):
        store.append_jsonl(tmp_path / "x.jsonl", ["not", "an", "object"])  # type: ignore[arg-type]


def test_read_json_default_and_corrupt(tmp_path: Path) -> None:
    assert store.read_json(tmp_path / "none.json", default={"d": 1}) == {"d": 1}
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    with pytest.raises(SchemaViolation):
        store.read_json(tmp_path / "bad.json")
