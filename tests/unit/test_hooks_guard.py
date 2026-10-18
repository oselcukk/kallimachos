"""PreToolUse guard: state files and managed note blocks only change through commands."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from kallimachos.cli import main
from kallimachos.hooks import guard
from kallimachos.paths import DataLayout

NOTE = """---
title: Example
---
# Example Author A (2021) - Fictional

My own text before.

<!-- kallimachos:begin full_reading model=example-reader -->
## Full reading
- Theorem 1 (p. 3): bounded.
<!-- kallimachos:end full_reading -->

My own text after.
"""


@pytest.fixture
def layout(tmp_path: Path) -> DataLayout:
    lay = DataLayout(tmp_path / "data")
    lay.state.mkdir(parents=True)
    return lay


@pytest.fixture
def note(tmp_path: Path) -> Path:
    path = tmp_path / "vault" / "Literature" / "Example-2021.md"
    path.parent.mkdir(parents=True)
    path.write_text(NOTE, encoding="utf-8")
    return path


def _write(path: Path | str, content: str = "x") -> dict[str, object]:
    return {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": content}}


def _edit(path: Path, old: str, new: str) -> dict[str, object]:
    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(path), "old_string": old, "new_string": new},
    }


@pytest.mark.parametrize(
    ("rel", "command"),
    [
        ("state/seen.jsonl", "run daily"),
        ("state/feedback.jsonl", "feedback add"),
        ("archive/catalog.jsonl", "archive run"),
        ("process/status.jsonl", "process"),
        ("process/cards/a1b2c3d4e5f6.json", "card-write"),
        ("process/reading_queue.jsonl", "queue"),
        ("runs/2026-10-18-daily.selection.json", "kallimachos select"),
        ("weights.json", "apply-weights"),
    ],
)
def test_state_files_are_denied_with_the_right_command(
    layout: DataLayout, rel: str, command: str
) -> None:
    reason = guard.decide(_write(layout.root / rel), layout)
    assert reason is not None and command in reason


@pytest.mark.parametrize(
    "rel", ["config.yaml", "profile.yaml", "archive/manual.txt", "daily/2026-10-18.md", "logs/x"]
)
def test_user_editable_data_files_are_allowed(layout: DataLayout, rel: str) -> None:
    assert guard.decide(_write(layout.root / rel), layout) is None


def test_files_outside_are_allowed(layout: DataLayout, tmp_path: Path) -> None:
    assert guard.decide(_write(tmp_path / "elsewhere.md", "hello"), layout) is None
    assert guard.decide({"tool_name": "Read", "tool_input": {}}, layout) is None
    assert guard.decide({"tool_name": "Write"}, layout) is None  # malformed -> fail open


def test_managed_block_edits(layout: DataLayout, note: Path) -> None:
    assert guard.decide(_edit(note, "My own text before.", "Changed."), layout) is None
    reason = guard.decide(_edit(note, "- Theorem 1 (p. 3): bounded.", "- changed"), layout)
    assert reason is not None and "note-write" in reason
    # an edit that spans the marker
    assert guard.decide(_edit(note, "before.\n\n<!-- kallimachos:begin", "x"), layout) is not None
    # introducing a fake marker
    fake = _edit(note, "My own text after.", "<!-- kallimachos:begin related -->")
    assert guard.decide(fake, layout) is not None
    multi = {
        "tool_name": "MultiEdit",
        "tool_input": {
            "file_path": str(note),
            "edits": [
                {"old_string": "My own text after.", "new_string": "ok"},
                {"old_string": "bounded.", "new_string": "unbounded."},
            ],
        },
    }
    assert guard.decide(multi, layout) is not None


def test_write_keeps_or_changes_blocks(layout: DataLayout, note: Path, tmp_path: Path) -> None:
    same_blocks = NOTE.replace("My own text after.", "Rewritten user paragraph.")
    assert guard.decide(_write(note, same_blocks), layout) is None
    changed = NOTE.replace("bounded.", "unbounded.")
    assert guard.decide(_write(note, changed), layout) is not None
    dropped = NOTE.split("<!-- kallimachos:begin")[0]
    assert guard.decide(_write(note, dropped), layout) is not None
    assert guard.decide(_write(tmp_path / "new.md", NOTE), layout) is not None  # forged note


def test_daily_block_is_exempt(layout: DataLayout, tmp_path: Path) -> None:
    daily = tmp_path / "Daily.md"
    text = (
        "<!-- kallimachos:begin daily run=2026-10-18-daily -->\n- [ ] 1 Title\n"
        "<!-- kallimachos:end -->\n"
    )
    daily.write_text(text, encoding="utf-8")
    assert guard.decide(_edit(daily, "- [ ] 1 Title", "- [x] 1 Title"), layout) is None


def test_apply_patch_codex(layout: DataLayout, note: Path, tmp_path: Path) -> None:
    def patch(body: str) -> dict[str, object]:
        return {"tool_name": "apply_patch", "cwd": str(tmp_path), "tool_input": {"command": body}}

    user_only = (
        "*** Begin Patch\n*** Update File: vault/Literature/Example-2021.md\n@@\n"
        "-My own text after.\n+Better text.\n*** End Patch\n"
    )
    assert guard.decide(patch(user_only), layout) is None
    inside = user_only.replace("-My own text after.", "-- Theorem 1 (p. 3): bounded.")
    assert guard.decide(patch(inside), layout) is not None
    delete = f"*** Begin Patch\n*** Delete File: {note}\n*** End Patch\n"
    assert guard.decide(patch(delete), layout) is not None
    state = f"*** Begin Patch\n*** Add File: {layout.seen_file}\n+{{}}\n*** End Patch\n"
    assert guard.decide(patch(state), layout) is not None


def test_shell_writes_to_state(layout: DataLayout) -> None:
    def bash(cmd: str) -> dict[str, object]:
        return {"tool_name": "Bash", "tool_input": {"command": cmd}}

    assert guard.decide(bash(f"echo x >> {layout.feedback_file}"), layout) is not None
    assert guard.decide(bash(f"cat {layout.feedback_file}"), layout) is None
    assert guard.decide(bash(f"kallimachos --data-dir {layout.root} doctor"), layout) is None
    assert guard.decide(bash(f"echo hi > {layout.root}/config.yaml"), layout) is None


def test_cli_guard_prints_deny_json_and_never_fails(
    layout: DataLayout, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_write(layout.seen_file))))
    assert main(["--data-dir", str(layout.root), "hooks", "guard"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert main(["--data-dir", str(layout.root), "hooks", "guard"]) == 0
    assert capsys.readouterr().out == ""
