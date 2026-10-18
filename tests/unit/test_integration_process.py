"""Integration (iii): the reading pipeline on a synthetic PDF with the real L7 and L7b modules,
the real store and a copied fake vault with its stub ``beyin.py``: extract -> card-write -> check
-> semantic-record -> note-write (Avenox sync) -> links -> queue add + sync (task-create).

Only ``pdftotext`` is replaced (the tool may be missing); everything else is the shipped code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kallimachos.archive.catalog import Catalog
from kallimachos.interfaces import CatalogEntry, ExitCode, WorkState, dump
from kallimachos.paths import DataLayout
from kallimachos.process import extract as extract_mod
from kallimachos.process import status as status_mod
from tests.unit.conftest_pdf import PAGE_1, PAGE_2, make_card, make_pdf
from tests.unit.test_integration_support import ANSWERS, edit_config, jsonl, run_cli
from tests.unit.test_process_notes_support import argv_log, body_md, make_vault

CHANNEL_FOLDER = "A-Analytic-number-theory"  # channel A of answers_qa.yaml


def test_reading_pipeline_to_vault_and_queue(
    _isolated_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    layout = DataLayout(_isolated_home)
    vault = make_vault(tmp_path)
    root = tmp_path / "library"
    pdf = make_pdf(root / CHANNEL_FOLDER / "Author_2019_Illustrative.pdf", [PAGE_1, PAGE_2])
    eid = extract_mod.eid_for(pdf)
    monkeypatch.setattr(extract_mod, "pdf_to_pages", lambda *_a, **_k: [PAGE_1, PAGE_2])

    run_cli("init", "--answers", str(ANSWERS))
    run_cli("profile", "approve")
    edit_config(
        layout,
        archive__enabled=True,
        archive__root=str(root),
        process__enabled=True,
        notes__target="avenox",
        notes__vault_path=str(vault),
        notes__literature_folder="Literature",
        notes__task_owner="Researcher",
    )
    entry = CatalogEntry(
        id=eid,
        keys=["doi:10.0000/example.9"],
        doi="10.0000/example.9",
        title="An illustrative result",
        authors=["Example Author A"],
        year=2019,
        channel="A",
        folder=CHANNEL_FOLDER,
        path=f"{CHANNEL_FOLDER}/{pdf.name}",
        filename=pdf.name,
        sha256=(eid + "0" * 64)[:64],
        pages=2,
    )
    Catalog.from_entries(layout.catalog_file, [entry]).save()
    run_cli("doctor")
    capsys.readouterr()

    # --- extract -> card -> check -> semantic -------------------------------------------------
    run_cli("process", "extract", eid)
    assert status_mod.current_state(layout, eid) is WorkState.TEXT_EXTRACTED
    assert extract_mod.load_pages(layout, eid) == [PAGE_1, PAGE_2]
    card_file = tmp_path / "card.json"
    card_file.write_text(json.dumps(dump(make_card(eid))), encoding="utf-8")
    run_cli("process", "card-write", eid, "--file", str(card_file))
    capsys.readouterr()
    run_cli("--json", "process", "check", eid)
    check = json.loads(capsys.readouterr().out)
    assert check["result"] == "check_passed"
    assert status_mod.current_state(layout, eid) is WorkState.CHECK_PASSED
    result = tmp_path / "semantic.json"
    result.write_text(
        json.dumps(
            {
                "schema": "kallimachos.semantic_result/1",
                "verifier_model": "example-verifier",
                "at": "2026-10-18T12:00:00+00:00",
                "result": "pass",
                "mismatches": [],
            }
        ),
        encoding="utf-8",
    )
    run_cli("process", "semantic-record", eid, "--file", str(result))
    assert status_mod.current_state(layout, eid) is WorkState.SEMANTIC_VERIFIED
    capsys.readouterr()

    # --- note-write into the vault, Avenox sync through the stub beyin.py -----------------------
    body = tmp_path / "note.md"
    body.write_text(body_md(), encoding="utf-8")
    run_cli("--json", "process", "note-write", eid, "--file", str(body))
    written = json.loads(capsys.readouterr().out)
    assert written["created"] is True and written["sync"] == "succeeded"
    note = Path(written["path"])
    assert note.is_file() and note.is_relative_to(vault / "Literature")
    assert written["frontmatter"]["eid"] == eid
    text = note.read_text(encoding="utf-8")
    assert text.startswith("---\n") and f"eid: {eid}" in text and "## 1. Citation" in text
    assert status_mod.current_state(layout, eid) is WorkState.NOTE_WRITTEN
    assert ["sync"] in [a[-1:] for a in argv_log(vault)] or any(
        "sync" in a for a in argv_log(vault)
    )

    # --- links and the reading queue ------------------------------------------------------------
    run_cli("process", "notes-index", "rebuild")
    assert eid in json.loads(layout.notes_index_file.read_text(encoding="utf-8"))
    capsys.readouterr()
    run_cli("--json", "process", "links", "--apply")
    links = json.loads(capsys.readouterr().out)
    assert links["apply"] is True
    assert status_mod.current_state(layout, eid) is WorkState.LINKED
    run_cli("process", "queue", "add", eid, "--kind", "read", "--note", "integration")
    assert "queued read: An illustrative result" in capsys.readouterr().out
    run_cli("--json", "process", "queue", "sync")
    synced = json.loads(capsys.readouterr().out)["synced"]
    assert [e["synced"]["avenox_task_id"] for e in synced] == [f"kallimachos-read-{eid}"]
    assert (vault / "tasks" / f"kallimachos-read-{eid}.md").is_file()
    assert any("task-create" in a for a in argv_log(vault))
    queue = jsonl(layout.reading_queue_file)
    assert queue and queue[-1]["synced"] is not None
    run_cli("--json", "process", "queue", "sync")  # nothing pending, no second task
    assert json.loads(capsys.readouterr().out)["synced"] == []
    run_cli("process", "status", "--summary")
    assert "linked: 1" in capsys.readouterr().out
    assert run_cli("doctor", expect=None) == ExitCode.OK
