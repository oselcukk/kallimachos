"""Shared fakes for the run tests (no tests here).

``run`` composes other libraries (store/runs/config/secrets/lock, feedback grammar/store, reading
queue). :func:`install` replaces them with small in-memory or temp-dir implementations through
``monkeypatch``; the run code under test is always the real one.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from kallimachos.config.schema import Config
from kallimachos.interfaces import (
    Candidate,
    FeedbackEntry,
    FeedbackTag,
    LockHeld,
    Record,
    RunKind,
    RunResult,
    Selection,
    UsageError,
    build,
    dump,
)
from kallimachos.paths import DataLayout
from kallimachos.profile.schema import Channel, Profile

REPO = Path(__file__).resolve().parents[2]
SCHEMAS = REPO / "schemas"
FIXTURES = REPO / "tests" / "fixtures"


def schema(name: str) -> dict[str, Any]:
    data = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def validate(data: Any, name: str) -> None:
    jsonschema.validate(data, schema(name))


@dataclass
class Env:
    root: Path
    layout: DataLayout
    config: Config
    secrets: dict[str, str] = field(default_factory=dict)
    lock_held: bool = False
    lock_events: list[str] = field(default_factory=list)
    feedback: list[FeedbackEntry] = field(default_factory=list)
    queue: list[dict[str, Any]] = field(default_factory=list)
    written_config: Config | None = None

    def write_profile(self, profile: Profile) -> None:
        self.layout.profile_file.parent.mkdir(parents=True, exist_ok=True)
        self.layout.profile_file.write_text(
            yaml.safe_dump(dump(profile), allow_unicode=True), encoding="utf-8"
        )

    def save_run(self, run: RunResult) -> None:
        path = self.layout.run_file(run.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = dump(run)
        validate(data, "run.schema.json")
        path.write_text(json.dumps(data), encoding="utf-8")

    def save_selection(self, sel: Selection) -> None:
        path = self.layout.selection_file(sel.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = dump(sel)
        validate(data, "selection.schema.json")
        path.write_text(json.dumps(data), encoding="utf-8")


def approved_profile() -> Profile:
    return Profile(
        status="approved",
        field="Example field",
        channels=[Channel(id="A", name="Topic A", folder="Topic-A")],
    )


def candidate(no: int, **kw: Any) -> Candidate:
    base: dict[str, Any] = {
        "no": no,
        "id": f"arXiv:2510.{no:05d}",
        "title": f"Example title number {no}",
        "authors": ["Example Author A", "Example Author B"],
        "abstract": f"First sentence of abstract {no}. Second sentence here. Third one.",
        "abstract_available": True,
        "channels": ["A"],
        "channel_names": ["Topic A"],
        "score_reasons": ["core phrase 'example topic' in title +6", "channel A"],
        "url": f"https://arxiv.org/abs/2510.{no:05d}",
        "arxiv": f"2510.{no:05d}",
    }
    base.update(kw)
    return Candidate(**base)


def make_run(
    n: int,
    *,
    kind: RunKind = RunKind.DAILY,
    day: _dt.date = _dt.date(2026, 10, 18),
    **overrides: Any,
) -> RunResult:
    rid = f"{day.isoformat()}-{kind.value}"
    cands = [candidate(i) for i in range(1, n + 1)]
    return RunResult(run_id=rid, kind=kind, date=day, candidates=cands, **overrides)


# ---------------------------------------------------------------------------
# fake L4 grammar (protocol only: "<n>[,<n>] <tag> [: note]", "#tag" / "-> tag")
# ---------------------------------------------------------------------------


def _tag_for(word: str) -> FeedbackTag | None:
    from kallimachos.feedback.grammar import SYNONYMS

    word = word.lower()
    for table in SYNONYMS.values():
        for tag, words in table.items():
            if word in words:
                return tag
    return None


def fake_parse(text: str, *, lang: str = "en") -> list[Any]:
    from kallimachos.feedback.grammar import ParsedReply

    out = []
    for line in text.splitlines():
        m = re.match(r"^\s*(H?\d+(?:\s*,\s*H?\d+)*)\s+(\S+?)(?::(\S+))?\s*(?::\s*(.*))?$", line)
        if not m:
            continue
        tag = _tag_for(m.group(2))
        items = [x.strip() for x in m.group(1).split(",")]
        if tag is None:
            out.append(ParsedReply(items=items, tag=FeedbackTag.GOOD, ambiguous=[m.group(2)]))
            continue
        out.append(ParsedReply(items=items, tag=tag, note=m.group(4), ask_target=m.group(3)))
    return out


def fake_markdown_tag(line: str) -> FeedbackTag | None:
    m = re.search(r"(?:#|->\s*|→\s*)(\w+)\s*$", line)
    return _tag_for(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config: Config | None = None) -> Env:
    root = tmp_path / "home"
    root.mkdir(exist_ok=True)
    env = Env(root=root, layout=DataLayout(root), config=config or Config())

    from kallimachos import secrets
    from kallimachos.config import load as config_load
    from kallimachos.feedback import grammar
    from kallimachos.feedback import store as fb_store
    from kallimachos.process import queue as process_queue
    from kallimachos.state import lock as state_lock
    from kallimachos.state import runs as state_runs
    from kallimachos.state import store

    # --- L1 store
    def write_json_atomic(
        path: Path, data: Any, *, schema: Mapping[str, Any] | None = None
    ) -> None:
        if schema is not None:
            jsonschema.validate(data, dict(schema))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")

    def read_json(path: Path, default: Any = None) -> Any:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    def write_text_atomic(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def append_jsonl(
        path: Path, record: Mapping[str, Any], *, schema: Mapping[str, Any] | None = None
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(record), ensure_ascii=False) + "\n")

    def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    yield json.loads(line)

    monkeypatch.setattr(store, "write_json_atomic", write_json_atomic)
    monkeypatch.setattr(store, "read_json", read_json)
    monkeypatch.setattr(store, "write_text_atomic", write_text_atomic)
    monkeypatch.setattr(store, "append_jsonl", append_jsonl)
    monkeypatch.setattr(store, "read_jsonl", read_jsonl)

    # --- L1 runs
    def load_run(layout: DataLayout, rid: str) -> RunResult:
        path = layout.run_file(rid)
        if not path.exists():
            raise UsageError(f"no run {rid}")
        return build(RunResult, json.loads(path.read_text(encoding="utf-8")))

    def load_selection(layout: DataLayout, rid: str) -> Selection | None:
        path = layout.selection_file(rid)
        if not path.exists():
            return None
        return build(Selection, json.loads(path.read_text(encoding="utf-8")))

    monkeypatch.setattr(state_runs, "load_run", load_run)
    monkeypatch.setattr(state_runs, "save_run", lambda layout, run: env.save_run(run))
    monkeypatch.setattr(state_runs, "load_selection", load_selection)
    monkeypatch.setattr(state_runs, "save_selection", lambda layout, sel: env.save_selection(sel))

    # --- L1 config / secrets / lock
    def load_config(
        path: Path | None = None, *, data_dir: str | None = None
    ) -> tuple[Config, DataLayout]:
        return env.config, env.layout

    def read_yaml(path: Path) -> dict[str, Any]:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        return data

    def write_config(path: Path, cfg: Config) -> None:
        env.written_config = cfg

    monkeypatch.setattr(config_load, "load_config", load_config)
    monkeypatch.setattr(config_load, "read_yaml", read_yaml)
    monkeypatch.setattr(config_load, "write_config", write_config)

    def resolve_ref(ref: str) -> str:
        try:
            return env.secrets[ref]
        except KeyError:
            raise UsageError(f"missing secret {ref}") from None

    monkeypatch.setattr(secrets, "resolve_ref", resolve_ref)

    class FakeLock:
        def __init__(self, path: Path, *, owner: str = "", stale_after_s: float = 0) -> None:
            self.path = path

        def acquire(self, *, timeout_s: float = 0.0) -> None:
            if env.lock_held:
                raise LockHeld(f"{self.path} is held")
            env.lock_events.append("acquire")

        def release(self) -> None:
            env.lock_events.append("release")

    monkeypatch.setattr(state_lock, "FileLock", FakeLock)

    # --- L4 grammar / feedback store
    monkeypatch.setattr(grammar, "parse", fake_parse)
    monkeypatch.setattr(grammar, "markdown_tag", fake_markdown_tag)

    def append_feedback(layout: DataLayout, entry: FeedbackEntry) -> bool:
        key = (entry.run_id, entry.candidate_id, entry.tag)
        if any((e.run_id, e.candidate_id, e.tag) == key for e in env.feedback):
            return False
        env.feedback.append(entry)
        return True

    def effective_tags(entries: Sequence[FeedbackEntry]) -> dict[str, FeedbackTag]:
        return {e.candidate_id: e.tag for e in entries}

    def resolve_item(run: RunResult, selection: Selection | None, item_no: str) -> tuple[str, str]:
        if selection is not None and item_no in selection.message_no:
            return selection.message_no[item_no], "selection"
        for c in run.candidates:
            if str(c.no) == item_no:
                return c.id, "run_no"
        raise UsageError(f"item {item_no} is not in {run.run_id}")

    monkeypatch.setattr(fb_store, "append_feedback", append_feedback)
    monkeypatch.setattr(fb_store, "load_feedback", lambda layout: list(env.feedback))
    monkeypatch.setattr(fb_store, "effective_tags", effective_tags)
    monkeypatch.setattr(fb_store, "resolve_item", resolve_item)

    # --- L7b queue
    def queue_add(layout: DataLayout, **kw: Any) -> Any:
        env.queue.append(kw)
        return None

    monkeypatch.setattr(process_queue, "queue_add", queue_add)
    return env


def args(**kw: Any) -> Any:
    """argparse.Namespace with the global flags defaulted."""
    import argparse

    base = {"json": False, "dry_run": False, "data_dir": None, "yes": False}
    base.update(kw)
    return argparse.Namespace(**base)


def record(n: int, **kw: Any) -> Record:
    base: dict[str, Any] = {
        "title": f"Example record {n}",
        "arxiv_id": f"2510.{n:05d}",
        "abstract": f"Abstract of record {n}. It has two sentences.",
        "url": f"https://arxiv.org/abs/2510.{n:05d}",
        "sources": {"arxiv"},
    }
    base.update(kw)
    return Record(**base)
