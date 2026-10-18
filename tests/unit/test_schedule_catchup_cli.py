"""``schedule run-missed`` day arithmetic, the ``schedule`` CLI and the doctor checks."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from kallimachos import cli_schedule
from kallimachos.cli import main
from kallimachos.config.schema import Config
from kallimachos.interfaces import ExitCode, RunKind, RunResult
from kallimachos.paths import DataLayout
from kallimachos.schedule import base, catchup, checks
from kallimachos.schedule.base import JobStatus
from kallimachos.state import runs as state_runs

D = _dt.date


def test_missed_days_arithmetic() -> None:
    today = D(2026, 10, 18)
    assert catchup.missed_days(None, today, max_days=3) == []
    assert catchup.missed_days(D(2026, 10, 17), today, max_days=3) == []
    assert catchup.missed_days(D(2026, 10, 18), today, max_days=3) == []
    assert catchup.missed_days(D(2026, 10, 15), today, max_days=3) == [
        D(2026, 10, 15) + _dt.timedelta(days=i) for i in (1, 2)
    ]
    # cap keeps the most recent days
    assert catchup.missed_days(D(2026, 10, 1), today, max_days=3) == [
        D(2026, 10, 15),
        D(2026, 10, 16),
        D(2026, 10, 17),
    ]
    assert catchup.missed_days(D(2026, 10, 1), today, max_days=0) == []
    # month and year boundaries
    assert catchup.missed_days(D(2026, 12, 30), D(2027, 1, 2), max_days=5) == [
        D(2026, 12, 31),
        D(2027, 1, 1),
    ]


def _save(layout: DataLayout, kind: RunKind, day: D) -> None:
    rid = state_runs.run_id(kind, day)
    state_runs.save_run(layout, RunResult(run_id=rid, kind=kind, date=day))


def _now(day: D, hh: int, mm: int = 0) -> _dt.datetime:
    return _dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=_dt.UTC)


def test_plan_merges_missed_days_into_one_run(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path / "data")
    cfg = Config()
    today = D(2026, 10, 14)  # Wednesday
    assert catchup.plan(layout, cfg, _now(today, 9)) == []  # never ran: nothing to catch up
    _save(layout, RunKind.DAILY, D(2026, 10, 10))
    steps = catchup.plan(layout, cfg, _now(today, 9))  # after 07:30 -> today is due
    assert len(steps) == 1
    step = steps[0]
    assert step.kind is RunKind.DAILY and step.day == today
    assert step.missed == [D(2026, 10, 12), D(2026, 10, 13), today]  # capped at 3
    assert step.window_days == 3
    early = catchup.plan(layout, cfg, _now(today, 6))  # before 07:30 -> up to yesterday
    assert early[0].day == D(2026, 10, 13) and early[0].missed[-1] == D(2026, 10, 13)
    _save(layout, RunKind.DAILY, today)
    assert catchup.plan(layout, cfg, _now(today, 9)) == []
    cfg.schedule.catch_up_missed = False
    _save(layout, RunKind.DAILY, D(2026, 10, 1))
    assert catchup.plan(layout, cfg, _now(D(2026, 10, 20), 9)) == []


def test_plan_weekly_slot(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path / "data")
    cfg = Config()
    _save(layout, RunKind.DAILY, D(2026, 10, 19))
    _save(layout, RunKind.WEEKLY, D(2026, 10, 11))  # last Sunday's weekly ran
    monday = D(2026, 10, 19)
    steps = catchup.plan(layout, cfg, _now(monday, 6))
    weekly = [s for s in steps if s.kind is RunKind.WEEKLY]
    assert weekly and weekly[0].day == D(2026, 10, 18) and weekly[0].window_days == 7
    sunday_early = _now(D(2026, 10, 18), 8)
    assert catchup.last_weekly_slot(sunday_early, "sun", "09:30") == D(2026, 10, 11)
    assert catchup.last_weekly_slot(_now(D(2026, 10, 18), 10), "sun", "09:30") == D(2026, 10, 18)


def test_cmd_run_missed_calls_run_once_with_window(tmp_path: Path) -> None:
    layout = DataLayout(Path(__import__("os").environ["KALLIMACHOS_HOME"]))
    _save(layout, RunKind.DAILY, D(2026, 10, 12))
    seen: list[argparse.Namespace] = []

    def fake_run(ns: argparse.Namespace) -> int:
        seen.append(ns)
        return 0

    args = argparse.Namespace(json=False, dry_run=False, data_dir=None, yes=False)
    code = cli_schedule.cmd_run_missed(args, run_command=fake_run, now=_now(D(2026, 10, 14), 6))
    assert code == 0 and len(seen) == 1
    ns = seen[0]
    assert ns.run_kind == "daily" and ns.date == "2026-10-13" and ns.window_days == 1
    assert ns.first_run is False and ns.rerun is False
    seen.clear()
    code = cli_schedule.cmd_run_missed(args, run_command=fake_run, now=_now(D(2026, 10, 14), 8))
    assert seen[0].date is None and seen[0].window_days == 2  # today, covering 13 and 14


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")


@pytest.fixture
def fake_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, Recorder, Path]:
    cfg = Config()
    cfg.schedule.backend = "launchd"
    recorder = Recorder()
    home = tmp_path / "home"
    layout = DataLayout(tmp_path / "data")

    def fake_load(path: Path | None = None, *, data_dir: str | None = None) -> Any:
        return cfg, layout

    real = base.get_backend

    def fake_backend(config: Config, **kw: Any) -> base.Backend:
        kw.update(home=home, runner=recorder)
        return real(config, **kw)

    monkeypatch.setattr(cli_schedule.config_load, "load_config", fake_load)
    monkeypatch.setattr(cli_schedule.sched_base, "get_backend", fake_backend)
    return cfg, recorder, home


def test_install_requires_approval_and_shows_files(
    fake_env: tuple[Config, Recorder, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _cfg, recorder, home = fake_env
    agents = home / "Library" / "LaunchAgents"
    assert main(["schedule", "install"]) == ExitCode.APPROVAL_REQUIRED
    out = capsys.readouterr()
    assert "io.github.oselcukk.kallimachos.daily.plist" in out.out and "<plist" in out.out
    assert "--yes" in out.err
    assert not agents.exists() and recorder.calls == []
    assert main(["--dry-run", "--yes", "schedule", "install"]) == ExitCode.OK
    assert not agents.exists()
    assert main(["--yes", "schedule", "install"]) == ExitCode.OK
    assert len(list(agents.glob("*.plist"))) == 3  # daily, weekly, catchup (archive off)
    assert any(c[1] == "bootstrap" for c in recorder.calls)
    capsys.readouterr()
    assert main(["--json", "schedule", "status"]) == ExitCode.OK
    status = json.loads(capsys.readouterr().out)
    assert {j["job"]: j["installed"] for j in status["jobs"]}["daily"] is True
    assert main(["schedule", "uninstall"]) == ExitCode.APPROVAL_REQUIRED
    assert list(agents.glob("*.plist"))
    assert main(["--yes", "schedule", "uninstall"]) == ExitCode.OK
    assert not list(agents.glob("*.plist"))


def test_install_refuses_unconfirmed_email(
    fake_env: tuple[Config, Recorder, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    cfg, recorder, home = fake_env
    cfg.notify.channels = ["email"]
    assert main(["--yes", "schedule", "install"]) == ExitCode.APPROVAL_REQUIRED
    assert "confirm-email" in capsys.readouterr().err
    assert recorder.calls == [] and not (home / "Library").exists()
    cfg.notify.email.confirmed_at = _dt.datetime(2026, 10, 18, tzinfo=_dt.UTC)
    assert main(["--yes", "schedule", "install"]) == ExitCode.OK


def test_install_json_lists_files(
    fake_env: tuple[Config, Recorder, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--json", "--dry-run", "schedule", "install", "--backend", "hermes"]) == 0
    data = json.loads(capsys.readouterr().out.split("kallimachos: dry run")[0])
    assert data["backend"] == "hermes" and data["installed"] is False
    assert any("hermes cron create" in line for line in data["guidance"])


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


class FakeBackend:
    name = "launchd"

    def __init__(self, statuses: list[JobStatus]) -> None:
        self.statuses = statuses

    def status(self, config: Config) -> list[JobStatus]:
        return self.statuses


def test_doctor_checks(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path / "data")
    cfg = Config()
    none = Config()
    none.schedule.backend = "none"
    assert checks.doctor_checks(layout, none)[0].ok
    assert checks.doctor_checks(layout, None) == []
    missing = checks.doctor_checks(layout, cfg, backend=FakeBackend([]))  # type: ignore[arg-type]
    assert not missing[0].ok and "schedule install" in missing[0].message
    good = [JobStatus(j, True, last_exit_code=0) for j in ("daily", "weekly", "catchup")]
    res = checks.doctor_checks(layout, cfg, backend=FakeBackend(good), today=D(2026, 10, 18))  # type: ignore[arg-type]
    assert all(c.ok for c in res), res
    _save(layout, RunKind.DAILY, D(2026, 10, 10))
    bad = [*good[:2], JobStatus("catchup", True, last_exit_code=6)]
    res = checks.doctor_checks(layout, cfg, backend=FakeBackend(bad), today=D(2026, 10, 18))  # type: ignore[arg-type]
    by = {c.name: c for c in res}
    assert not by["schedule last exit"].ok and "catchup exit 6" in by["schedule last exit"].message
    assert not by["schedule runs"].ok
    cfg.notify.channels = ["email"]
    res = checks.doctor_checks(layout, cfg, backend=FakeBackend(good), today=D(2026, 10, 11))  # type: ignore[arg-type]
    assert not {c.name: c for c in res}["schedule e-mail"].ok


def test_approval_hint_repeats_the_options(
    fake_env: tuple[Config, Recorder, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["schedule", "install", "--backend", "launchd"]) == ExitCode.APPROVAL_REQUIRED
    err = capsys.readouterr().err
    assert "kallimachos --yes schedule install --backend launchd" in err
