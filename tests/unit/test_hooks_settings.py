"""Hook installation keeps every other hook (Avenox's included), backs up, and is idempotent.

Each test runs with a temporary HOME; the real ``~/.claude`` and ``~/.codex`` are never read or
written.
"""

from __future__ import annotations

import datetime as _dt
import io
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from kallimachos.cli import main
from kallimachos.config.schema import Config
from kallimachos.hooks import checks as hook_checks
from kallimachos.hooks import settings as hs
from kallimachos.hooks.summary import build_summary
from kallimachos.interfaces import (
    ExitCode,
    FeedbackEntry,
    ReadingQueueEntry,
    RunKind,
    RunResult,
    Selection,
    UsageError,
    dump,
)
from kallimachos.paths import DataLayout
from kallimachos.state import runs as state_runs
from kallimachos.state import store

AVENOX = {
    "type": "command",
    "command": "python3 /vault/.claude/scripts/beyin_v3_hook.py --vault /vault --harness claude",
    "timeout": 5,
}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    return h


@pytest.fixture
def layout() -> DataLayout:
    return DataLayout(Path(__import__("os").environ["KALLIMACHOS_HOME"]))


def _existing() -> dict[str, Any]:
    return {
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {
            "SessionStart": [{"hooks": [AVENOX]}],
            "PostToolUse": [{"matcher": "Edit|Write", "hooks": [AVENOX]}],
        },
    }


def test_merge_keeps_foreign_hooks_and_is_idempotent(layout: DataLayout) -> None:
    groups = hs.our_groups("claude", "/py", layout, session_start=True, guard=True)
    once = hs.merged(_existing(), groups)
    twice = hs.merged(once, groups)
    assert once == twice
    assert once["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert once["hooks"]["SessionStart"][0] == {"hooks": [AVENOX]}
    assert once["hooks"]["PostToolUse"] == [{"matcher": "Edit|Write", "hooks": [AVENOX]}]
    assert hs.installed_events(once) == ["SessionStart", "PreToolUse"]
    stripped, removed = hs.strip_ours(once)
    assert removed == 2 and stripped == _existing()
    cmd = groups["PreToolUse"]["hooks"][0]["command"]
    assert "hooks guard --harness claude" in cmd and str(layout.root) in cmd
    assert hs.is_ours({"command": cmd}) and not hs.is_ours(AVENOX)
    codex = hs.our_groups("codex", "/py", layout, session_start=False, guard=True)
    assert list(codex) == ["PreToolUse"] and "apply_patch" in codex["PreToolUse"]["matcher"]


def test_settings_paths(tmp_path: Path) -> None:
    h, p = tmp_path / "h", tmp_path / "p"
    assert hs.settings_path("claude", "user", home=h, project=p) == h / ".claude" / "settings.json"
    assert hs.settings_path("claude", "local", home=h, project=p).name == "settings.local.json"
    assert hs.settings_path("codex", "user", home=h, project=p) == h / ".codex" / "hooks.json"
    assert hs.settings_path("codex", "project", home=h, project=p) == p / ".codex" / "hooks.json"
    with pytest.raises(UsageError):
        hs.settings_path("codex", "local", home=h, project=p)


def test_invalid_settings_are_not_touched(tmp_path: Path) -> None:
    bad = tmp_path / "settings.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(UsageError):
        hs.read_settings(bad)
    assert bad.read_text(encoding="utf-8") == "{not json"


def test_codex_inline_hooks_block_install(tmp_path: Path, layout: DataLayout) -> None:
    codex = tmp_path / "proj" / ".codex"
    codex.mkdir(parents=True)
    (codex / "config.toml").write_text('[[hooks.PreToolUse]]\nmatcher = "Bash"\n', "utf-8")
    plan = hs.plan_install(
        "codex", "project", python="/py", layout=layout, home=tmp_path, project=tmp_path / "proj"
    )
    assert plan.problem and "inline" in plan.problem
    (codex / "config.toml").write_text("[features]\nhooks = false\n", "utf-8")
    plan = hs.plan_install(
        "codex", "project", python="/py", layout=layout, home=tmp_path, project=tmp_path / "proj"
    )
    assert plan.problem is None and any("hooks = false" in w for w in plan.warnings)
    assert any("/hooks" in w for w in plan.warnings)


def test_cli_install_uninstall_with_backup(
    home: Path, layout: DataLayout, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps(_existing()), encoding="utf-8")
    argv = ["hooks", "install", "--target", "claude", "--scope", "user"]
    assert main(argv) == ExitCode.APPROVAL_REQUIRED
    assert json.loads(settings.read_text(encoding="utf-8")) == _existing()
    assert main(["--yes", *argv]) == ExitCode.OK
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert hs.installed_events(data) == ["SessionStart", "PreToolUse"]
    backups = list(settings.parent.glob("settings.json.bak-kallimachos-*"))
    assert len(backups) == 1 and json.loads(backups[0].read_text("utf-8")) == _existing()
    record = hs.read_record(layout)
    assert record == [
        {
            "target": "claude",
            "scope": "user",
            "path": str(settings),
            "events": "SessionStart,PreToolUse",
        }
    ]
    assert main(["--yes", *argv]) == ExitCode.OK  # idempotent
    assert json.loads(settings.read_text(encoding="utf-8")) == data
    capsys.readouterr()
    assert main(["--json", "hooks", "status"]) == ExitCode.OK
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["events"] == ["SessionStart", "PreToolUse"]
    off = ["hooks", "uninstall", "--target", "claude", "--scope", "user"]
    assert main(["--yes", *off]) == ExitCode.OK
    assert json.loads(settings.read_text(encoding="utf-8")) == _existing()
    assert hs.read_record(layout) == []


def test_cli_codex_project_install(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    argv = ["--yes", "hooks", "install", "--target", "codex", "--scope", "project"]
    assert main([*argv, "--project", str(proj)]) == ExitCode.OK
    data = json.loads((proj / ".codex" / "hooks.json").read_text("utf-8"))
    assert data["hooks"]["PreToolUse"][0]["matcher"] == "apply_patch|Edit|Write|Bash"
    assert "trust" in capsys.readouterr().out
    assert not (home / ".codex").exists()


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


def _run(layout: DataLayout, day: _dt.date, main_ids: list[str]) -> str:
    rid = state_runs.run_id(RunKind.DAILY, day)
    state_runs.save_run(layout, RunResult(run_id=rid, kind=RunKind.DAILY, date=day))
    state_runs.save_selection(layout, Selection(run_id=rid, main=main_ids))
    return rid


def test_summary_reports_runs_feedback_queue(layout: DataLayout) -> None:
    cfg = Config()
    layout.config_file.parent.mkdir(parents=True, exist_ok=True)
    layout.config_file.write_text("schema_version: 1\n", encoding="utf-8")
    today = _dt.date(2026, 10, 18)
    text = build_summary(layout, cfg, today=today)
    assert text is not None and "No scan yet" in text
    old = _run(layout, _dt.date(2026, 10, 16), ["a"])
    new = _run(layout, _dt.date(2026, 10, 17), ["a", "b"])
    store.append_jsonl(
        layout.feedback_file,
        dump(FeedbackEntry(at=_dt.datetime(2026, 10, 17, tzinfo=_dt.UTC), run_id=old)),
    )
    store.append_jsonl(
        layout.reading_queue_file,
        dump(
            ReadingQueueEntry(
                at=_dt.datetime(2026, 10, 17, tzinfo=_dt.UTC),
                kind="read",
                title="Fictional",
                candidate_id="doi:10.0000/example.1",
            )
        ),
    )
    text = build_summary(layout, cfg, today=today)
    assert text is not None
    assert f"Last scan {new} (yesterday, ok): 2 paper(s)" in text
    assert "Messages without feedback: 1 (" + new in text and old not in text.split("feedback:")[1]
    assert "Reading queue: 1" in text
    cfg.hooks.summary_max_chars = 120
    short = build_summary(layout, cfg, today=today)
    assert short is not None and len(short) <= 120 and short.endswith("...")


def test_summary_silent_without_installation(tmp_path: Path) -> None:
    assert build_summary(DataLayout(tmp_path / "none"), Config()) is None


def test_cli_session_start_output(
    layout: DataLayout, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    layout.root.mkdir(parents=True, exist_ok=True)
    layout.config_file.write_text("schema_version: 1\n", encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO('{"hook_event_name": "SessionStart"}'))
    assert main(["hooks", "session-start", "--harness", "codex"]) == 0
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert ctx.startswith("Kallimachos: ") and len(ctx) <= 600
    layout.config_file.write_text("unknown_key: 1\n", encoding="utf-8")  # invalid -> silent
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert main(["hooks", "session-start"]) == 0
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_checks_run_the_installed_commands(home: Path, layout: DataLayout) -> None:
    assert hook_checks.doctor_checks(layout, Config())[0].ok  # not installed: optional
    argv = ["--yes", "hooks", "install", "--target", "claude", "--scope", "user"]
    assert main([*argv, "--python", sys.executable]) == ExitCode.OK
    calls: list[list[str]] = []

    def executor(cmd: Sequence[str], stdin: str) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        payload = json.loads(stdin)
        out = ""
        if (
            payload.get("tool_name") == "Write"
            and "seen.jsonl" in payload["tool_input"]["file_path"]
        ):
            out = json.dumps({"hookSpecificOutput": {"permissionDecision": "deny"}})
        return subprocess.CompletedProcess(list(cmd), 0, out, "")

    res = hook_checks.doctor_checks(layout, Config(), executor=executor)
    assert len(res) == 1 and res[0].ok, res
    assert {c[-3] for c in calls} == {"session-start", "guard"}

    def broken(cmd: Sequence[str], stdin: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    res = hook_checks.doctor_checks(layout, Config(), executor=broken)
    assert not res[0].ok and "did not deny" in res[0].message
    settings = home / ".claude" / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    res = hook_checks.doctor_checks(layout, Config(), executor=executor)
    assert not res[0].ok and "missing" in res[0].message


def test_doctor_live_guard_subprocess(home: Path, layout: DataLayout) -> None:
    """One real run of the installed guard command (no network, temporary data dir)."""
    argv = ["--yes", "hooks", "install", "--target", "claude", "--scope", "user"]
    assert main([*argv, "--python", sys.executable]) == ExitCode.OK
    res = hook_checks.doctor_checks(layout, Config())
    assert res[0].ok, res


def test_install_through_a_symlinked_settings_file_updates_the_target(
    home: Path, layout: DataLayout, capsys: pytest.CaptureFixture[str]
) -> None:
    dotfiles = home / "dotfiles" / "settings.json"
    dotfiles.parent.mkdir(parents=True)
    dotfiles.write_text(json.dumps(_existing()), encoding="utf-8")
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.symlink_to(dotfiles)
    argv = ["--yes", "hooks", "install", "--target", "claude", "--scope", "user"]
    assert main(argv) == ExitCode.OK
    assert settings.is_symlink(), "the symlink must survive the install"
    data = json.loads(dotfiles.read_text(encoding="utf-8"))
    assert hs.installed_events(data) == ["SessionStart", "PreToolUse"]
    assert data["permissions"] == _existing()["permissions"]
    backups = list(dotfiles.parent.glob("settings.json.bak-kallimachos-*"))
    assert len(backups) == 1 and json.loads(backups[0].read_text("utf-8")) == _existing()
    assert "->" in capsys.readouterr().out
    # a second, unchanged install writes no second backup
    assert main(argv) == ExitCode.OK
    assert len(list(dotfiles.parent.glob("settings.json.bak-kallimachos-*"))) == 1
    assert "nothing changed" in capsys.readouterr().out


def test_dangling_symlinked_settings_file_is_refused(home: Path, layout: DataLayout) -> None:
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.symlink_to(home / "missing" / "settings.json")
    with pytest.raises(UsageError):
        hs.write_settings(settings, {"hooks": {}})


def test_backups_never_overwrite_each_other(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    first = hs.backup(path)
    path.write_text('{"a": 2}', encoding="utf-8")
    second = hs.backup(path)
    assert first is not None and second is not None and first != second
    assert first.read_text(encoding="utf-8") == '{"a": 1}'
    assert second.read_text(encoding="utf-8") == '{"a": 2}'
    assert hs.backup(tmp_path / "absent.json") is None
