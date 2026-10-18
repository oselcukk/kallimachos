"""Shared fakes and builders for the note tests (``test_process_notes_*``).

Neighbouring modules (``state.store``, ``process.semantic/card/status/chunk``) are replaced by
minimal in-memory/file fakes through the ``fake_lanes`` fixture, which every note test module
imports. This module holds no tests of its own.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from kallimachos.config.schema import Config, NotesConfig
from kallimachos.interfaces import (
    Card,
    CardCitation,
    CardItem,
    CatalogEntry,
    ItemType,
    SemanticResult,
    StatusEntry,
    WorkState,
    build,
    dump,
)
from kallimachos.paths import DataLayout
from kallimachos.process import card as card_mod
from kallimachos.process import chunk as chunk_mod
from kallimachos.process import semantic as semantic_mod
from kallimachos.process import status as status_mod
from kallimachos.profile.schema import Channel, Profile
from kallimachos.state import store
from kallimachos.state.machine import assert_transition

FIXTURE_VAULT = Path(__file__).resolve().parents[1] / "fixtures" / "vault"
EID_A = "a1b2c3d4e5f6"
EID_B = "0123456789ab"  # the eid of the fixture note Other-2020-Second-Example.md
EID_C = "fedcba987654"


# ---------------------------------------------------------------------------
# Fake state.store (store contract, minimal)
# ---------------------------------------------------------------------------


def _nfc(data: Any) -> Any:
    if isinstance(data, str):
        return unicodedata.normalize("NFC", data)
    if isinstance(data, dict):
        return {_nfc(k): _nfc(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_nfc(v) for v in data]
    return data


def fake_append_jsonl(
    path: Path, record: Mapping[str, Any], *, schema: Mapping[str, Any] | None = None
) -> None:
    if schema is not None:
        import jsonschema

        jsonschema.validate(dict(record), dict(schema))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_nfc(dict(record)), ensure_ascii=False, default=str) + "\n")


def fake_read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def fake_write_json_atomic(
    path: Path, data: Any, *, schema: Mapping[str, Any] | None = None
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(_nfc(data), ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    os.replace(tmp, path)


def fake_read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def fake_write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(unicodedata.normalize("NFC", text), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Fake processing-core functions
# ---------------------------------------------------------------------------


def fake_latest_semantic(layout: DataLayout, eid: str) -> SemanticResult | None:
    path = layout.semantic_dir / f"{eid}.json"
    data = fake_read_json(path)
    return build(SemanticResult, data) if data else None


def fake_load_card(layout: DataLayout, eid: str) -> Card | None:
    data = fake_read_json(layout.cards_dir / f"{eid}.json")
    return build(Card, data) if data else None


def fake_current_states(layout: DataLayout) -> dict[str, StatusEntry]:
    out: dict[str, StatusEntry] = {}
    for raw in fake_read_jsonl(layout.status_file):
        entry = build(StatusEntry, raw)
        out[entry.eid] = entry
    return out


def fake_record_state(
    layout: DataLayout, eid: str, state: WorkState, *, by: str, at: _dt.datetime | None = None
) -> StatusEntry:
    current = fake_current_states(layout).get(eid)
    assert_transition(current.state if current else None, state)
    entry = StatusEntry(eid=eid, state=state, at=at or _dt.datetime.now(tz=_dt.UTC), by=by)
    fake_append_jsonl(layout.status_file, dump(entry))
    return entry


@dataclass
class FakeChunk:
    number: int
    channel: str = "EX"
    eids: list[str] = field(default_factory=list)
    groups: list[list[str]] = field(default_factory=list)
    books: list[str] = field(default_factory=list)


def fake_load_chunk(layout: DataLayout, number: int) -> FakeChunk:
    data = fake_read_json(layout.chunks_dir / str(number) / "list.json", default={})
    eids = data.get("eids", data) if isinstance(data, dict) else data
    return FakeChunk(number=number, eids=list(eids))


@pytest.fixture
def fake_lanes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the L1 store and L7 process stubs with the fakes above."""
    monkeypatch.setattr(store, "append_jsonl", fake_append_jsonl)
    monkeypatch.setattr(store, "read_jsonl", fake_read_jsonl)
    monkeypatch.setattr(store, "write_json_atomic", fake_write_json_atomic)
    monkeypatch.setattr(store, "read_json", fake_read_json)
    monkeypatch.setattr(store, "write_text_atomic", fake_write_text_atomic)
    monkeypatch.setattr(semantic_mod, "latest_semantic", fake_latest_semantic)
    monkeypatch.setattr(card_mod, "load_card", fake_load_card)
    monkeypatch.setattr(status_mod, "current_states", fake_current_states)
    monkeypatch.setattr(status_mod, "record_state", fake_record_state)
    monkeypatch.setattr(chunk_mod, "load_chunk", fake_load_chunk)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def make_vault(tmp_path: Path, *, avenox: bool = True) -> Path:
    """Copy the fixture vault; without ``avenox`` the beyin files are removed (plain folder)."""
    vault = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, vault)
    state_dir = tmp_path / "beyin-state"
    shutil.copytree(vault / "state", state_dir)
    shutil.rmtree(vault / "state")
    runtime = vault / ".beyin-runtime.json"
    runtime.write_text(
        runtime.read_text(encoding="utf-8").replace("__STATE_DIR__", state_dir.as_posix()),
        encoding="utf-8",
    )
    if not avenox:
        (vault / "beyin.py").unlink()
        runtime.unlink()
    return vault


def make_config(vault: Path, *, avenox: bool = True, **notes: Any) -> Config:
    cfg = Config()
    cfg.notes = NotesConfig(
        target="avenox" if avenox else "plain",
        vault_path=str(vault),
        literature_folder="Literature",
        task_owner="Researcher",
        **notes,
    )
    cfg.notes.frontmatter.project = "example-project"
    cfg.process.models.reader = "example-reader"
    return cfg


def make_profile() -> Profile:
    return Profile(
        status="approved",
        field="example-field",
        channels=[Channel(id="EX", name="Example channel", folder="Example-Channel")],
    )


def make_layout(tmp_path: Path) -> DataLayout:
    layout = DataLayout(tmp_path / "data")
    for d in layout.required_dirs():
        d.mkdir(parents=True, exist_ok=True)
    return layout


def catalog_entry(
    eid: str = EID_A,
    *,
    authors: list[str] | None = None,
    year: int | None = 2021,
    title: str = "On the Distribution of Fictional Zeta Sums",
    path: str = "Example-Channel/01-Topic/Author_2021_Fictional.pdf",
    channel: str = "EX",
) -> CatalogEntry:
    return CatalogEntry(
        id=eid,
        keys=[f"doi:10.0000/example.{eid[:4]}"],
        doi=f"10.0000/example.{eid[:4]}",
        title=title,
        authors=authors if authors is not None else ["Example Author A", "Example Author B"],
        year=year,
        channel=channel,
        folder="Example-Channel/01-Topic",
        path=path,
        filename=Path(path).name,
    )


def write_catalog(layout: DataLayout, *entries: CatalogEntry) -> None:
    layout.catalog_file.parent.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        fake_append_jsonl(layout.catalog_file, dump(entry))


def item(
    type_: ItemType = ItemType.THEOREM,
    label: str = "Theorem 1",
    page: int = 3,
    **extra: Any,
) -> CardItem:
    return CardItem(
        type=type_,
        label=label,
        page=page,
        quote="This is a verbatim quotation of at least forty characters from the work.",
        statement="Statement of the item in the reader's words.",
        **extra,
    )


def card(
    eid: str = EID_A,
    *,
    authors: list[str] | None = None,
    year: int | None = 2021,
    title: str = "On the Distribution of Fictional Zeta Sums",
    items: list[CardItem] | None = None,
) -> Card:
    return Card(
        eid=eid,
        path=f"Example-Channel/01-Topic/{eid}.pdf",
        pages=12,
        citation=CardCitation(
            authors=authors if authors is not None else ["Example Author A", "Example Author B"],
            year=year,
            title=title,
        ),
        summary="Fictional summary.",
        items=items if items is not None else [item()],
    )


def write_card(layout: DataLayout, c: Card) -> None:
    fake_write_json_atomic(layout.cards_dir / f"{c.eid}.json", dump(c))


def write_semantic(layout: DataLayout, eid: str, result: str = "pass") -> None:
    sem = SemanticResult(
        eid=eid,
        verifier_model="example-verifier",
        at=_dt.datetime(2026, 1, 3, tzinfo=_dt.UTC),
        result=result,  # type: ignore[arg-type]
    )
    fake_write_json_atomic(layout.semantic_dir / f"{eid}.json", dump(sem))


def set_state(layout: DataLayout, eid: str, *states: WorkState) -> None:
    """Append a chain of states (entry state first) without transition checks."""
    for st in states:
        fake_append_jsonl(
            layout.status_file,
            dump(StatusEntry(eid=eid, state=st, at=_dt.datetime.now(tz=_dt.UTC), by="test")),
        )


def chain_to(state: WorkState) -> tuple[WorkState, ...]:
    """The linear process chain from ``downloaded`` up to ``state``."""
    order = (
        WorkState.DOWNLOADED,
        WorkState.TEXT_EXTRACTED,
        WorkState.CARD_WRITTEN,
        WorkState.CHECK_PASSED,
        WorkState.SEMANTIC_VERIFIED,
        WorkState.NOTE_WRITTEN,
        WorkState.LINKED,
        WorkState.CLUSTER_SUMMARIZED,
    )
    return order[: order.index(state) + 1]


def body_md(
    *,
    title: str = (
        "Example Author A, Example Author B (2021) - On the Distribution of Fictional Zeta Sums"
    ),
    relation: str | None = "- No direct relation to the profile channels. [inference]",
    extra: str = "",
) -> str:
    sections = [
        "## 1. Citation\n\nExample Author A, Example Author B (2021). Fictional Journal 1, 1-12.",
        "## 2. Summary\n\nA fictional summary.",
        "## 3. Structure\n\nFive sections.",
        "## 4. Definitions\n\n- Definition 1 (p. 2): a fictional zeta sum.",
        "## 5. Main results\n\n- Theorem 1 (p. 3): the sum is bounded for every real s > 1.",
        "## 6. Method\n\nElementary estimates.",
        "## 7. Open problems and questions\n\n- Question 2 (p. 10): is the bound sharp?",
        "## 8. Relation to prior work\n\nBuilds on earlier fictional work.",
        "## 9. Caveats\n\n[Kallimachos observation] none.",
        "## 10. Source file\n\nExample-Channel/01-Topic/Author_2021_Fictional.pdf",
        "## 11. Not found\n\nnone",
    ]
    text = f"# {title}\n\n" + "\n\n".join(sections) + "\n"
    if relation is not None:
        text += f"\n## Relation to my work (inference)\n\n{relation}\n"
    return text + extra


def argv_log(vault: Path) -> list[list[str]]:
    log = vault / ".fake-beyin-argv.jsonl"
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


__all__ = [
    "EID_A",
    "EID_B",
    "EID_C",
    "argv_log",
    "body_md",
    "card",
    "catalog_entry",
    "chain_to",
    "fake_lanes",
    "item",
    "make_config",
    "make_layout",
    "make_profile",
    "make_vault",
    "set_state",
    "write_card",
    "write_catalog",
    "write_semantic",
]
