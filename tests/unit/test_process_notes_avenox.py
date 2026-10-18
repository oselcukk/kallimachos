"""``process.avenox``: detection, ``sync``/``task-create`` through the stub
``beyin.py``, the single receipt command line and the literature-folder index check."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kallimachos.process import avenox
from tests.unit.test_process_notes_support import argv_log, make_vault


def test_detect_requires_both_files(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    info = avenox.detect(vault)
    assert info is not None
    assert info.beyin_py == vault / "beyin.py" and info.version == "3.11.0"
    (vault / ".beyin-runtime.json").unlink()
    assert avenox.detect(vault) is None
    assert avenox.detect(make_vault(tmp_path / "plain", avenox=False)) is None


def test_python_command_matches_agents_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(avenox.sys, "platform", "win32")
    assert avenox.python_command() == ["py", "-3"]
    monkeypatch.setattr(avenox.sys, "platform", "darwin")
    assert avenox.python_command() == ["python3"]


def test_sync_runs_in_vault_and_reports_conflict(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    info = avenox.detect(vault)
    assert info is not None
    result = avenox.sync(info)
    assert result["status"] == "succeeded" and result["indexed"] == 3
    assert argv_log(vault) == [["--json", "sync"]]
    (vault / ".fake-sync-conflict").write_text("", encoding="utf-8")
    assert avenox.sync(info)["status"] == "conflict"


def test_run_surfaces_avenox_error_json(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    info = avenox.detect(vault)
    assert info is not None
    result = avenox.run(info, "frobnicate")
    assert result["status"] == "error" and result["error"] == "UsageError"
    assert "frobnicate" in result["message"] and result["exit_code"] == 1


def test_task_create_refuses_existing_task(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    info = avenox.detect(vault)
    assert info is not None
    payload = {
        "source": "tasks/kallimachos-read-x.md",
        "text": "read: x",
        "metadata": {
            "id": "kallimachos-read-x",
            "title": "x",
            "kind": "task",
            "revision": 1,
            "status": "inbox",
            "owner": "Researcher",
            "visibility": "internal",
            "facts": {"candidate_id": "x", "url": None, "kind": "read"},
        },
    }
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps(payload), encoding="utf-8")
    result = avenox.task_create(info, task_file)
    assert (
        result["id"] == "kallimachos-read-x" and result["source"] == "tasks/kallimachos-read-x.md"
    )
    assert result["status"] == "inbox" and result["owner"] == "Researcher"
    assert avenox.task_exists(info, "kallimachos-read-x")
    again = avenox.task_create(info, task_file)
    assert again["status"] == "error" and "exists" in again["message"]


def test_receipt_command_matches_beyin_skill(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    info = avenox.detect(vault)
    assert info is not None
    summary = tmp_path / "summary.txt"
    refs = [vault / "Literature" / "Example-Channel" / "01-Topic" / "A.md", Path("Literature/B.md")]
    argv = avenox.receipt_command(
        info,
        harness="claude",
        event_id="kallimachos-process-2026-01-03-chunk1",
        summary_file=summary,
        refs=refs,
        session="abc123",
    )
    assert argv == [
        *avenox.python_command(),
        str(vault / "beyin.py"),
        "receipt",
        "--harness",
        "claude",
        "--event-id",
        "kallimachos-process-2026-01-03-chunk1",
        "--summary-file",
        str(summary),
        "--ref",
        "Literature/Example-Channel/01-Topic/A.md",
        "--ref",
        "Literature/B.md",
        "--session",
        "abc123",
    ]
    no_session = avenox.receipt_command(
        info, harness="codex", event_id="e", summary_file=summary, refs=[], session=None
    )
    assert "--session" not in no_session and "--ref" not in no_session
    with pytest.raises(ValueError, match="harness"):
        avenox.receipt_command(
            info, harness="unknown", event_id="e", summary_file=summary, refs=[], session=None
        )
    with pytest.raises(ValueError, match="event_id"):
        avenox.receipt_command(
            info, harness="claude", event_id=" ", summary_file=summary, refs=[], session=None
        )


def test_literature_folder_indexed(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    info = avenox.detect(vault)
    assert info is not None
    assert avenox.excluded_dirs(info) == ["3rdparty", "Drafts"]
    assert avenox.literature_folder_indexed(info, "Literature") == (True, "indexed")
    ok, reason = avenox.literature_folder_indexed(info, "Drafts/Literature")
    assert not ok and "excluded_dirs" in reason
    ok, reason = avenox.literature_folder_indexed(info, "Private/Papers")
    assert not ok and "sensitive" in reason
    ok, reason = avenox.literature_folder_indexed(info, "Research/Özel")
    assert not ok and "sensitive" in reason
    assert avenox.literature_folder_indexed(info, "../outside")[0] is False
    assert avenox.literature_folder_indexed(info, "notes/literature")[0] is True


def test_sensitive_name_folding() -> None:
    assert avenox.sensitive_name("MÜŞTERİLER")
    assert avenox.sensitive_name("secrets")
    assert avenox.sensitive_name("my-passwords")
    assert not avenox.sensitive_name("Literature")
    assert not avenox.sensitive_name("Privateer")  # whole-word match only
