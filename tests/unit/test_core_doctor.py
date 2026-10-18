"""doctor: clean on an empty data dir, safe repairs only, secret presence only."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
import yaml

from kallimachos.cli import main
from kallimachos.config.schema import Config
from kallimachos.doctor import DoctorCheck, run_doctor
from kallimachos.interfaces import ExitCode, RunResult, SourceStats
from kallimachos.paths import DataLayout
from kallimachos.state.lock import FileLock
from kallimachos.state.runs import save_run

SECRET = "Lm4" + "p8" * 12


def by_name(report_checks: list[object], name: str) -> object:
    return next(c for c in report_checks if getattr(c, "name", None) == name)


def test_clean_on_empty_data_dir(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path / "empty")
    report = run_doctor(layout, None)
    assert report.ok, report.checks
    assert not layout.root.exists()  # without --fix nothing is created


def test_cli_doctor_on_empty_home(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor"]) == ExitCode.OK
    assert "all checks passed" in capsys.readouterr().out
    assert main(["--json", "doctor"]) == ExitCode.OK
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and {c["name"] for c in data["checks"]} >= {"config", "locks"}


def test_fix_creates_dirs_and_clears_only_stale_locks(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path / "home")
    layout.locks.mkdir(parents=True)
    stale = layout.lock_file("run")
    stale.write_text(json.dumps({"pid": 999_999_999, "acquired_at": time.time()}), "utf-8")
    live = FileLock(layout.lock_file("archive"), owner="archive run")
    live.acquire()
    user_file = layout.root / "notes.md"
    user_file.write_text("mine", encoding="utf-8")
    try:
        before = run_doctor(layout, None)
        assert not before.ok
        assert "stale: run" in by_name(before.checks, "locks").message  # type: ignore[attr-defined]
        assert stale.exists()
        after = run_doctor(layout, None, fix=True)
        assert after.ok
        assert not stale.exists()
        assert layout.lock_file("archive").exists()
        assert all(p.is_dir() for p in layout.required_dirs())
        assert user_file.read_text(encoding="utf-8") == "mine"
    finally:
        live.release()


def test_dry_run_fix_repairs_nothing(_isolated_home: Path) -> None:
    layout = DataLayout(_isolated_home)
    assert main(["--dry-run", "doctor", "--fix"]) == ExitCode.OK
    assert not layout.state.exists()
    assert main(["doctor", "--fix"]) == ExitCode.OK
    assert layout.state.is_dir()


def test_invalid_config_is_reported_not_raised(_isolated_home: Path) -> None:
    (_isolated_home / "config.yaml").write_text("bogus_key: 1\n", encoding="utf-8")
    assert main(["doctor"]) == ExitCode.UNEXPECTED
    report = run_doctor(DataLayout(_isolated_home), None)
    assert not by_name(report.checks, "config").ok  # type: ignore[attr-defined]


def test_secret_presence_without_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = DataLayout(tmp_path)
    data = {
        "sources": {"openalex": {"api_key_ref": "env:KALLI_OA"}},
        "notify": {"telegram": {"bot_token_ref": "env:KALLI_TG"}},
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("KALLI_OA", SECRET)
    report = run_doctor(layout, None)
    secrets_check = by_name(report.checks, "secrets")
    msg = secrets_check.message  # type: ignore[attr-defined]
    assert "sources.openalex.api_key_ref: present" in msg
    assert "notify.telegram.bot_token_ref: absent" in msg
    assert SECRET not in msg
    assert not secrets_check.ok  # type: ignore[attr-defined]
    monkeypatch.setenv("KALLIMACHOS_HOME", str(tmp_path))
    main(["--json", "doctor"])
    assert SECRET not in capsys.readouterr().out


def test_profile_and_state_checks(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path)
    (tmp_path / "profile.yaml").write_text("status: approved\nfield: example\n", "utf-8")
    layout.state.mkdir(parents=True)
    layout.seen_file.write_text('{"keys": []}\n', encoding="utf-8")  # missing required runs
    report = run_doctor(layout, Config())
    assert by_name(report.checks, "profile").ok  # type: ignore[attr-defined]
    state = by_name(report.checks, "state")
    assert not state.ok and "seen.jsonl:1" in state.message  # type: ignore[attr-defined]


def test_last_run_and_source_errors(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path)
    save_run(
        layout,
        RunResult(
            run_id="2026-10-18-daily",
            errors=["arxiv: 3 tries failed"],
            sources={
                "arxiv": SourceStats(errors=["export.arxiv.org: 3 tries failed"]),
                "crossref": SourceStats(count=4),
            },
        ),
    )
    report = run_doctor(layout, Config())
    assert by_name(report.checks, "last run").ok  # type: ignore[attr-defined]
    src = by_name(report.checks, "source errors")
    assert not src.ok and "arxiv" in src.message  # type: ignore[attr-defined]


def test_avenox_check_degrades_gracefully(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    config = Config()
    config.notes.vault_path = str(vault)
    report = run_doctor(DataLayout(tmp_path / "home"), config)
    check = by_name(report.checks, "avenox")
    assert check.ok  # type: ignore[attr-defined]


def test_extension_checks_are_appended_and_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Optional modules plug in through ``doctor_checks(layout, config)``: their checks are
    appended; a missing module contributes nothing; a crashing one becomes a failing check."""
    import sys
    import types

    from kallimachos import doctor

    good = types.ModuleType("fake_ext_good")
    good.doctor_checks = lambda layout, config: [  # type: ignore[attr-defined]
        doctor.DoctorCheck("schedule", True, f"config={config}")
    ]
    bad = types.ModuleType("fake_ext_bad")

    def boom(layout: object, config: object) -> list[object]:
        raise RuntimeError("broken extension")

    bad.doctor_checks = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_ext_good", good)
    monkeypatch.setitem(sys.modules, "fake_ext_bad", bad)
    monkeypatch.setattr(
        doctor, "EXTENSION_MODULES", ("fake_ext_good", "fake_ext_bad", "fake_ext_missing")
    )
    report = run_doctor(DataLayout(tmp_path / "empty"), None, fix=True)
    names = [c.name for c in report.checks]
    assert names.count("schedule") == 1 and "fake_ext_bad" in names
    assert by_name(report.checks, "schedule").message == "config=None"
    failing = by_name(report.checks, "fake_ext_bad")
    assert failing.ok is False and "broken extension" in failing.message


def test_cache_entries_with_secrets_are_reported_and_removed_by_fix(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path / "home")
    layout.http_cache.mkdir(parents=True)
    old = layout.http_cache / ("a" * 40 + ".json")
    old.write_text(
        json.dumps(
            {
                "stored_at": 1.0,
                "kind": "single",
                "url": "https://api.example.org/x?api_key=OLDKEY",
                "status": 200,
                "headers": {},
                "body_b64": "",
            }
        ),
        encoding="utf-8",
    )
    check = by_name(run_doctor(layout, None).checks, "cache secrets")
    assert isinstance(check, DoctorCheck)
    assert not check.ok and check.fixable and "1 cache entry" in check.message
    check = by_name(run_doctor(layout, None, fix=True).checks, "cache secrets")
    assert isinstance(check, DoctorCheck) and check.fixed and not old.exists()
    check = by_name(run_doctor(layout, None).checks, "cache secrets")
    assert isinstance(check, DoctorCheck) and check.ok


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX modes")
def test_permissions_check_and_fix(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path / "home")
    layout.ensure_private_dirs()
    layout.config_file.write_text("language: en\n", encoding="utf-8")
    assert (layout.root.stat().st_mode & 0o777) == 0o700
    layout.root.chmod(0o755)
    leaky = layout.state / "seen.jsonl"
    leaky.write_text("", encoding="utf-8")
    leaky.chmod(0o644)
    check = by_name(run_doctor(layout, None).checks, "permissions")
    assert isinstance(check, DoctorCheck) and not check.ok and check.fixable
    check = by_name(run_doctor(layout, None, fix=True).checks, "permissions")
    assert isinstance(check, DoctorCheck) and check.fixed
    assert (layout.root.stat().st_mode & 0o777) == 0o700
    assert (leaky.stat().st_mode & 0o777) == 0o600
    check = by_name(run_doctor(layout, None).checks, "permissions")
    assert isinstance(check, DoctorCheck) and check.ok


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX modes")
def test_store_creates_owner_only_files(tmp_path: Path) -> None:
    from kallimachos.state import store

    jsonl = tmp_path / "a.jsonl"
    store.append_jsonl(jsonl, {"a": 1})
    assert (jsonl.stat().st_mode & 0o777) == 0o600
    js = tmp_path / "b.json"
    store.write_json_atomic(js, {"a": 1})
    assert (js.stat().st_mode & 0o777) == 0o600
