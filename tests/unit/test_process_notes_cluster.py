"""``process cluster start|write|open-problems``."""

from __future__ import annotations

from pathlib import Path

import pytest

from kallimachos.config.schema import Config
from kallimachos.interfaces import (
    ApprovalRequired,
    GraphEdge,
    ItemStatus,
    ItemType,
    UsageError,
    WorkState,
    dump,
)
from kallimachos.paths import DataLayout
from kallimachos.process import cluster
from kallimachos.process.notes_index import parse_frontmatter
from kallimachos.state import store
from tests.unit.test_process_notes_support import (
    EID_A,
    EID_B,
    argv_log,
    card,
    chain_to,
    fake_lanes,
    item,
    make_config,
    make_layout,
    make_vault,
    set_state,
    write_card,
)

pytestmark = pytest.mark.usefixtures("fake_lanes")
_ = fake_lanes

FOLDER = Path("Example-Channel/01-Topic")


def _setup(
    tmp_path: Path, *, state: WorkState = WorkState.LINKED
) -> tuple[Path, DataLayout, Config]:
    vault = make_vault(tmp_path)
    layout = make_layout(tmp_path)
    note_a = vault / "Literature" / FOLDER / "A-2021-First.md"
    note_a.write_text(f"---\neid: {EID_A}\n---\n# First (2021)\n", encoding="utf-8")
    set_state(layout, EID_A, *chain_to(state))
    set_state(layout, EID_B, *chain_to(WorkState.LINKED))
    write_card(
        layout,
        card(
            EID_A,
            items=[item(), item(ItemType.OPEN_PROBLEM, "Problem 1", 10, concreteness="concrete")],
        ),
    )
    write_card(
        layout,
        card(
            EID_B,
            items=[
                item(ItemType.QUESTION, "Question 2", 4, status=ItemStatus.OPEN),
                item(ItemType.OPEN_PROBLEM, "Problem 3", 5, status=ItemStatus.SOLVED_ELSEWHERE),
                item(ItemType.CLAIMED_SOLUTION, "Theorem 4", 6, target="Problem 1"),
            ],
        ),
    )
    edge = GraphEdge(source_eid=EID_B, target_eid=EID_A, key="2", method="doi", score=99.0)
    store.append_jsonl(layout.graph_file, dump(edge))
    return vault, layout, make_config(vault)


def test_cluster_start_gate_below_linked(tmp_path: Path) -> None:
    _vault, layout, config = _setup(tmp_path, state=WorkState.NOTE_WRITTEN)
    with pytest.raises(ApprovalRequired, match=EID_A):
        cluster.cluster_start(FOLDER, layout, config)


def test_cluster_start_packet(tmp_path: Path) -> None:
    vault, layout, config = _setup(tmp_path)
    packet = cluster.cluster_start(FOLDER, layout, config)
    assert packet.folder == vault / "Literature" / FOLDER
    assert set(packet.eids) == {EID_A, EID_B}
    assert len(packet.note_paths) == 2 and all(
        not p.name.startswith("_") for p in packet.note_paths
    )
    assert [e.source_eid for e in packet.edges] == [EID_B]
    assert len(packet.card_items[EID_B]) == 3
    assert "## 1. Map of the cluster" in packet.skeleton_md
    assert "## 5. Suggested concept notes" in packet.skeleton_md
    assert "[comment]" in packet.skeleton_md
    assert (
        f"<!-- {EID_A} Literature/Example-Channel/01-Topic/A-2021-First.md -->"
        in packet.skeleton_md
    )


def _summary(
    intro: str = (
        "This summary rests on the notes, the list file and the citation graph only; PDFs "
        "were not re-read; every bullet links to a note; unsourced remarks carry [comment]."
    ),
) -> str:
    return (
        "# Cluster summary: Example-Channel/01-Topic\n\n"
        f"{intro}\n\n"
        "## 1. Map of the cluster\n\n### 1.1 Main lines\n\n"
        "- [[Literature/Example-Channel/01-Topic/A-2021-First]] opens the line.\n"
        "- Influence is unclear. [comment]\n\n"
        "## 2. Notation glossary\n\n"
        "- $S(x)$: fictional sum, [[Literature/Example-Channel/01-Topic/A-2021-First]].\n\n"
        "## 3. Numerical results\n\nNone recorded.\n\n"
        "## 4. Errors and corrections\n\nNone.\n\n"
        "## 5. Suggested concept notes\n\n- Fictional zeta sums (proposal only). [comment]\n"
    )


def test_cluster_write_validation_errors(tmp_path: Path) -> None:
    _vault, layout, config = _setup(tmp_path)
    with pytest.raises(UsageError, match="frontmatter"):
        cluster.cluster_write(
            FOLDER, "---\nx: 1\n---\n" + _summary(), layout, config, verified_by=None
        )
    with pytest.raises(UsageError, match="missing cluster sections"):
        cluster.cluster_write(
            FOLDER,
            _summary().replace("## 2. Notation glossary", "## 2. Other"),
            layout,
            config,
            verified_by=None,
        )
    with pytest.raises(UsageError, match="method paragraph"):
        cluster.cluster_write(FOLDER, _summary(intro=""), layout, config, verified_by=None)
    with pytest.raises(UsageError, match=r"\[comment\] tag convention"):
        cluster.cluster_write(
            FOLDER, _summary(intro="Based on notes only."), layout, config, verified_by=None
        )
    with pytest.raises(UsageError, match="link to no note"):
        cluster.cluster_write(
            FOLDER,
            _summary() + "\n- An unsourced bullet without a link.\n",
            layout,
            config,
            verified_by=None,
        )
    with pytest.raises(UsageError, match="fake links"):
        cluster.cluster_write(
            FOLDER,
            _summary() + "\nMatrix [[a,0],[0,b]] [comment]\n",
            layout,
            config,
            verified_by=None,
        )


def test_cluster_write_writes_frontmatter_state_and_sync(tmp_path: Path) -> None:
    vault, layout, config = _setup(tmp_path)
    path = cluster.cluster_write(FOLDER, _summary(), layout, config, verified_by="example-verifier")
    assert path == vault / "Literature" / FOLDER / "_Cluster-Summary.md"
    text = path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    assert fm["kallimachos"] == "cluster/1" and fm["cluster"] == "Example-Channel/01-Topic"
    assert fm["work_count"] == 2 and fm["kind"] == "note" and fm["project"] == "example-project"
    assert set(fm) == {
        "kind",
        "project",
        "visibility",
        "updated_at",
        "cluster",
        "work_count",
        "kallimachos",
    }
    assert body.startswith("<!-- kallimachos:verified_by example-verifier -->")
    states = list(store.read_jsonl(layout.status_file))
    summarized = {s["eid"] for s in states if s["state"] == WorkState.CLUSTER_SUMMARIZED.value}
    assert summarized == {EID_A, EID_B}
    assert [a[-1] for a in argv_log(vault)] == ["sync"]
    # rewriting our own summary is allowed; a foreign file at that path is not
    cluster.cluster_write(FOLDER, _summary(), layout, config, verified_by=None)
    path.write_text("# user file\n", encoding="utf-8")
    with pytest.raises(ApprovalRequired):
        cluster.cluster_write(FOLDER, _summary(), layout, config, verified_by=None)


def test_open_problems_table_excludes_solved_elsewhere(tmp_path: Path) -> None:
    _vault, layout, config = _setup(tmp_path)
    table = cluster.open_problems_table(FOLDER, layout, config)
    assert "| # | Work | Problem (author's words, p.) | Concreteness | Status | Evidence |" in table
    rows = [ln for ln in table.splitlines() if ln.startswith("| ") and not ln.startswith("| #")]
    # Problem 1 of A is claimed solved by B (claimed_solution target + B cites A): excluded;
    # Problem 3 of B is solved_elsewhere by card status: excluded; Question 2 of B stays.
    assert len(rows) == 1
    assert "Other-2020-Second-Example" in rows[0] and "(p. 4)" in rows[0] and "| open |" in rows[0]
