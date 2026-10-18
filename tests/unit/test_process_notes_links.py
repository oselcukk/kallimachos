"""``process links`` and ``links fix-fake``."""

from __future__ import annotations

from pathlib import Path

import pytest

from kallimachos.config.schema import Config
from kallimachos.interfaces import ApprovalRequired, GraphEdge, WorkState, dump
from kallimachos.paths import DataLayout
from kallimachos.process import links
from kallimachos.process.notes_index import NoteRef, NotesIndex
from kallimachos.state import store
from tests.unit.test_process_notes_support import (
    EID_A,
    EID_B,
    EID_C,
    chain_to,
    fake_lanes,
    fake_write_json_atomic,
    make_config,
    make_layout,
    make_vault,
    set_state,
)

pytestmark = pytest.mark.usefixtures("fake_lanes")
_ = fake_lanes

NOTE_A = "Literature/Example-Channel/01-Topic/A-2021-First.md"
NOTE_B = "Literature/Example-Channel/01-Topic/Other-2020-Second-Example.md"
NOTE_C = "Literature/Example-Channel/02-Other/C-2019-Third.md"


def _index(layout: DataLayout) -> NotesIndex:
    index = NotesIndex(layout.notes_index_file)
    index.set(EID_A, NoteRef(path=NOTE_A, title="First", updated_at="2026-01-01"))
    index.set(EID_B, NoteRef(path=NOTE_B, title="Second", updated_at="2026-01-02"))
    index.set(EID_C, NoteRef(path=NOTE_C, title="Third", updated_at="2026-01-03"))
    index.save()
    return index


def _edge(src: str, tgt: str, key: str = "3", score: float = 95.0) -> GraphEdge:
    return GraphEdge(source_eid=src, target_eid=tgt, key=key, method="doi", score=score, text="ref")


def _setup(tmp_path: Path) -> tuple[Path, DataLayout, Config, NotesIndex]:
    vault = make_vault(tmp_path)
    layout = make_layout(tmp_path)
    (vault / NOTE_A).write_text(
        f"---\neid: {EID_A}\n---\n# First\n\nBody of A.\n", encoding="utf-8"
    )
    (vault / NOTE_C).parent.mkdir(parents=True, exist_ok=True)
    (vault / NOTE_C).write_text(f"---\neid: {EID_C}\n---\n# Third\n", encoding="utf-8")
    for raw in (dump(_edge(EID_A, EID_B)), dump(_edge(EID_A, EID_C, key="7"))):
        store.append_jsonl(layout.graph_file, raw)
    store.append_jsonl(layout.graph_uncertain_file, dump(_edge(EID_B, EID_C, key="1", score=80)))
    for eid in (EID_A, EID_B, EID_C):
        set_state(layout, eid, *chain_to(WorkState.NOTE_WRITTEN))
    return vault, layout, make_config(vault), _index(layout)


def test_related_targets_skip_existing_links_but_not_the_automatic_block(tmp_path: Path) -> None:
    _vault, _layout, _config, index = _setup(tmp_path)
    edges = [_edge(EID_A, EID_B), _edge(EID_A, EID_C, key="7"), _edge(EID_A, EID_A, key="9")]
    assert links.related_targets(EID_A, edges, index, "# First\n") == [
        (NOTE_B, ["3"]),
        (NOTE_C, ["7"]),
    ]
    manual = "# First\n\nSee [[Literature/Example-Channel/01-Topic/Other-2020-Second-Example]].\n"
    assert links.related_targets(EID_A, edges, index, manual) == [(NOTE_C, ["7"])]
    manual_basename = (
        "# First\n\nSee [[Other-2020-Second-Example|alias]] and "
        "[Third](../02-Other/C-2019-Third.md).\n"
    )
    assert links.related_targets(EID_A, edges, index, manual_basename) == []
    automatic = (
        "# First\n\n<!-- kallimachos:begin related_notes -->\n## Related\n"
        "- [[Literature/Example-Channel/01-Topic/Other-2020-Second-Example]]\n"
        "<!-- kallimachos:end related_notes -->\n"
    )
    assert links.related_targets(EID_A, edges, index, automatic) == [
        (NOTE_B, ["3"]),
        (NOTE_C, ["7"]),
    ]


def test_apply_links_dry_run_then_apply_is_idempotent(tmp_path: Path) -> None:
    vault, layout, config, _index = _setup(tmp_path)
    before = (vault / NOTE_A).read_text(encoding="utf-8")
    dry = links.apply_links(layout, config, apply=False, force=False)
    assert [p.name for p in dry.updated] == ["A-2021-First.md"]
    assert (vault / NOTE_A).read_text(encoding="utf-8") == before
    assert dry.uncertain_report is not None and not dry.uncertain_report.exists()

    report = links.apply_links(layout, config, apply=True, force=False)
    text = (vault / NOTE_A).read_text(encoding="utf-8")
    assert text.startswith(before.rstrip("\n"))
    assert text.count("kallimachos:begin related_notes") == 1
    assert "## Related notes in the archive (from the citation graph, automatic)" in text
    assert (
        "- [[Literature/Example-Channel/01-Topic/Other-2020-Second-Example]] — references [3]"
        in text
    )
    assert "- [[Literature/Example-Channel/02-Other/C-2019-Third]] — references [7]" in text
    assert report.uncertain_report is not None and report.uncertain_report.is_file()
    uncertain = report.uncertain_report.read_text(encoding="utf-8")
    assert NOTE_B in uncertain and NOTE_C in uncertain
    assert "C-2019-Third" not in (vault / NOTE_B).read_text(
        encoding="utf-8"
    )  # uncertain never written

    again = links.apply_links(layout, config, apply=True, force=False)
    assert again.updated == []
    assert (vault / NOTE_A).read_text(encoding="utf-8") == text
    states = list(store.read_jsonl(layout.status_file))
    linked = {s["eid"] for s in states if s["state"] == WorkState.LINKED.value}
    assert linked == {EID_A, EID_B, EID_C}


def test_apply_links_markdown_style_for_plain_folder(tmp_path: Path) -> None:
    vault, layout, _config, _index = _setup(tmp_path)
    config = make_config(vault, avenox=False)
    links.apply_links(layout, config, apply=True, force=False)
    text = (vault / NOTE_A).read_text(encoding="utf-8")
    assert "- [Second](Other-2020-Second-Example.md)" in text
    assert "- [Third](../02-Other/C-2019-Third.md)" in text


def test_apply_links_gate_on_active_chunk(tmp_path: Path) -> None:
    _vault, layout, config, _index = _setup(tmp_path)
    fake_write_json_atomic(layout.chunks_dir / "1" / "list.json", {"eids": [EID_A, "aaaaaaaaaaaa"]})
    with pytest.raises(ApprovalRequired, match="aaaaaaaaaaaa"):
        links.apply_links(layout, config, apply=True, force=False)
    report = links.apply_links(layout, config, apply=True, force=True)
    assert len(report.updated) == 1


def test_apply_links_closed_chunk_does_not_gate(tmp_path: Path) -> None:
    _vault, layout, config, _index = _setup(tmp_path)
    set_state(layout, "bbbbbbbbbbbb", *chain_to(WorkState.LINKED))
    fake_write_json_atomic(layout.chunks_dir / "2" / "list.json", {"eids": ["bbbbbbbbbbbb"]})
    links.apply_links(layout, config, apply=False, force=False)


# ---------------------------------------------------------------------------
# fix-fake
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Matrix [[a,0],[0,b]] here.", "Matrix [ [a,0],[0,b] ] here."),
        ("Nested [[x [y] z]] here.", "Nested [ [x [y] z] ] here."),
        ("Interval [[0,1]]", "Interval [ [0,1] ]"),
        ("Fake [x(yz)](x,y,z) link.", "Fake [x(yz)] (x,y,z) link."),
        ("Real [doc](https://example.org/a) link.", "Real [doc](https://example.org/a) link."),
        ("Mail [me](mailto:a@example.org).", "Mail [me](mailto:a@example.org)."),
        (
            "Obsidian [n](obsidian://open?vault=x&file=y).",
            "Obsidian [n](obsidian://open?vault=x&file=y).",
        ),
        (
            "Anchor [s](#section) and file [f](notes/x.md).",
            "Anchor [s](#section) and file [f](notes/x.md).",
        ),
        ("Unclosed [[matrix on a line", "Unclosed [ [matrix on a line"),
        ("Real [[Smith-2020-Title]] link.", "Real [[Smith-2020-Title]] link."),
        ("Alias [[Smith-2020-Title|Smith]] link.", "Alias [[Smith-2020-Title|Smith]] link."),
    ],
)
def test_fix_fake_links_cases(text: str, expected: str) -> None:
    fixed, fixes = links.fix_fake_links(text)
    assert fixed == expected
    assert bool(fixes) == (fixed != text)


def test_fix_fake_links_preserves_code_blocks_and_known_targets() -> None:
    text = "```\n[[a,0],[0,b]]\n```\nInline `[[a,0],[0,b]]` stays.\nBut [[a,0],[0,b]] goes.\n"
    fixed, fixes = links.fix_fake_links(text)
    assert (
        fixed
        == "```\n[[a,0],[0,b]]\n```\nInline `[[a,0],[0,b]]` stays.\nBut [ [a,0],[0,b] ] goes.\n"
    )
    assert len(fixes) == 1
    text = "See [[Weird (Title)]] note."
    assert links.fix_fake_links(text)[0] == "See [ [Weird (Title)] ] note."
    assert links.fix_fake_links(text, known_targets={"Weird (Title)"})[0] == text


def test_fix_fake_in_folder_reports_and_applies(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    lit = vault / "Literature"
    bad = lit / "Example-Channel" / "01-Topic" / "Bad-2021-Note.md"
    bad.write_text("# Bad\n\n[[a,0],[0,b]] and [[Other-2020-Second-Example]]\n", encoding="utf-8")
    report = links.fix_fake_in_folder(lit, apply=False, vault_root=vault)
    assert [p.name for p in report.updated] == ["Bad-2021-Note.md"] and len(report.fixes) == 1
    assert "[[a,0],[0,b]]" in bad.read_text(encoding="utf-8")
    links.fix_fake_in_folder(lit, apply=True, vault_root=vault)
    assert (
        bad.read_text(encoding="utf-8")
        == "# Bad\n\n[ [a,0],[0,b] ] and [[Other-2020-Second-Example]]\n"
    )
