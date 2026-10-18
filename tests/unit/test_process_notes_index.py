"""``process notes-index rebuild|find`` and the frontmatter helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kallimachos.interfaces import SchemaViolation
from kallimachos.process.notes_index import (
    NoteRef,
    NotesIndex,
    eid_of,
    iter_notes,
    note_title,
    parse_frontmatter,
    split_frontmatter,
)
from tests.unit.test_process_notes_support import EID_A, EID_B, fake_lanes, make_layout, make_vault

pytestmark = pytest.mark.usefixtures("fake_lanes")
_ = fake_lanes


def test_split_and_parse_frontmatter() -> None:
    text = "---\nkind: note\neid: 0123456789ab\n---\n# Title\n\nBody\n"
    fm, body = split_frontmatter(text)
    assert fm == "kind: note\neid: 0123456789ab\n" and body == "# Title\n\nBody\n"
    assert parse_frontmatter(text) == ({"kind": "note", "eid": "0123456789ab"}, "# Title\n\nBody\n")
    assert split_frontmatter("# no frontmatter\n") == (None, "# no frontmatter\n")
    assert split_frontmatter("---\nunterminated\n") == (None, "---\nunterminated\n")
    assert eid_of('---\neid: "0123456789ab"\n---\n') == "0123456789ab"
    assert eid_of("---\neid: nope\n---\n") is None
    assert (
        note_title(text, "fallback") == "Title" and note_title("plain\n", "fallback") == "fallback"
    )


def test_iter_notes_skips_system_files(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    lit = vault / "Literature"
    (lit / "_Reading-Queue.md").write_text("# q\n", encoding="utf-8")
    (lit / "Example-Channel" / "_Cluster-Summary.md").write_text("# c\n", encoding="utf-8")
    names = [p.name for p in iter_notes(lit)]
    assert names == ["Author-2019-Example-Title.md", "Other-2020-Second-Example.md"]


def test_rebuild_and_find(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    layout = make_layout(tmp_path)
    (vault / "Literature/Example-Channel/01-Topic/Smith-Jones-2021-Fictional-Sums.md").write_text(
        f"---\neid: {EID_A}\nzotero: zotero://select/library/items/ABCD1234\n"
        "citekey: smith2021fictional\n---\n"
        "# Example Smith, Example Jones (2021) - Fictional Sums\n",
        encoding="utf-8",
    )
    index = NotesIndex(layout.notes_index_file)
    assert index.rebuild(vault / "Literature", vault) == 2  # the legacy note has no eid
    index.save()
    data = json.loads(layout.notes_index_file.read_text(encoding="utf-8"))
    assert data[EID_B]["path"] == "Literature/Example-Channel/01-Topic/Other-2020-Second-Example.md"
    assert data[EID_B]["updated_at"] == "2026-01-02" and "zotero_key" not in data[EID_B]
    assert (
        data[EID_A]["zotero_key"] == "ABCD1234" and data[EID_A]["citekey"] == "smith2021fictional"
    )

    loaded = NotesIndex.load(layout.notes_index_file)
    assert [eid for eid, _ in loaded.find("smith")] == [EID_A]
    assert [eid for eid, _ in loaded.find("Jones", 2021)] == [EID_A]
    assert loaded.find("jones", 2020) == []
    assert [eid for eid, _ in loaded.find("Other")] == [EID_B]
    assert [eid for eid, _ in loaded.find("example")] == [EID_B, EID_A][::-1] or len(
        loaded.find("example")
    ) == 2
    assert loaded.find("") == []
    assert loaded.by_path(data[EID_A]["path"]) == EID_A


def test_set_and_load_validate(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    index = NotesIndex(layout.notes_index_file)
    with pytest.raises(SchemaViolation):
        index.set("bad", NoteRef(path="x.md", title="x", updated_at="2026-01-01"))
    index.set(EID_A, NoteRef(path="x.md", title="x", updated_at="2026-01-01"))
    index.save()
    layout.notes_index_file.write_text('{"zzz": {"path": "x"}}', encoding="utf-8")
    with pytest.raises(SchemaViolation):
        NotesIndex.load(layout.notes_index_file)
