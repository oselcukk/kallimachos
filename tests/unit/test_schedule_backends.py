"""Scheduler back-ends render their service files and call the system tools through a fake runner.

Nothing here touches the real launchd / Task Scheduler / systemd / crontab / Hermes / GitHub: every
file goes to ``tmp_path`` and every command is recorded by ``FakeRunner``.
"""

from __future__ import annotations

import datetime as _dt
import os
import plistlib
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from kallimachos.config.schema import Config
from kallimachos.interfaces import UsageError
from kallimachos.paths import DataLayout
from kallimachos.schedule import base, cron, gh_actions, hermes, launchd, systemd, taskscheduler

PY = "/opt/example python/bin/python3"
NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


class FakeRunner:
    def __init__(self, outputs: dict[str, tuple[int, str]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.outputs = outputs or {}

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append(argv)
        key = " ".join(argv[:2])
        code, out = self.outputs.get(key, (0, ""))
        return subprocess.CompletedProcess(argv, code, out, "")


@pytest.fixture
def config() -> Config:
    cfg = Config()
    cfg.archive.enabled = True
    return cfg


@pytest.fixture
def layout(tmp_path: Path) -> DataLayout:
    return DataLayout(tmp_path / "data dir")


def test_job_specs_follow_config(config: Config) -> None:
    jobs = [s.job for s in base.job_specs(config)]
    assert jobs == ["daily", "weekly", "archive", "catchup"]
    config.archive.enabled = False
    config.schedule.catch_up_missed = False
    assert [s.job for s in base.job_specs(config)] == ["daily", "weekly"]
    with pytest.raises(UsageError):
        base.parse_hhmm("25:00")


def test_local_backend_resolution() -> None:
    assert base.local_backend_name("darwin") == "launchd"
    assert base.local_backend_name("win32") == "taskscheduler"
    assert base.local_backend_name("linux", has_systemd=True) == "systemd"
    assert base.local_backend_name("linux", has_systemd=False) == "cron"
    cfg = Config()
    cfg.schedule.backend = "none"
    with pytest.raises(UsageError):
        base.get_backend(cfg)
    assert base.get_backend(cfg, backend="hermes").name == "hermes"
    assert base.get_backend(cfg, backend="local", platform="darwin").name == "launchd"


# ---------------------------------------------------------------------------
# launchd
# ---------------------------------------------------------------------------


def test_launchd_render_matches_appendix_c1(
    tmp_path: Path, config: Config, layout: DataLayout
) -> None:
    be = launchd.LaunchdBackend(home=tmp_path / "home", runner=FakeRunner())
    files = {f.job: f for f in be.render(config, layout, python=PY)}
    assert set(files) == {"daily", "weekly", "archive", "catchup"}
    daily = plistlib.loads(files["daily"].content.encode())
    assert files["daily"].path.name == "io.github.oselcukk.kallimachos.daily.plist"
    assert files["daily"].path.parent == tmp_path / "home" / "Library" / "LaunchAgents"
    assert daily["Label"] == "io.github.oselcukk.kallimachos.daily"
    assert daily["ProgramArguments"] == [
        PY,
        "-m",
        "kallimachos",
        "--data-dir",
        str(layout.root),
        "run",
        "daily",
    ]
    assert daily["StartCalendarInterval"] == {"Hour": 7, "Minute": 30}
    assert daily["RunAtLoad"] is False
    assert daily["ProcessType"] == "Background"
    assert daily["EnvironmentVariables"] == {"KALLIMACHOS_HOME": str(layout.root)}
    assert daily["StandardOutPath"] == str(layout.logs / "launchd-daily.out")
    weekly = plistlib.loads(files["weekly"].content.encode())
    assert weekly["StartCalendarInterval"] == {"Hour": 9, "Minute": 30, "Weekday": 0}
    archive = plistlib.loads(files["archive"].content.encode())
    assert archive["StartCalendarInterval"] == {"Hour": 6, "Minute": 30}
    assert archive["ProgramArguments"][-2:] == ["archive", "run"]
    catchup = plistlib.loads(files["catchup"].content.encode())
    assert catchup["RunAtLoad"] is True and "StartCalendarInterval" not in catchup
    assert catchup["ProgramArguments"][-2:] == ["schedule", "run-missed"]


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil only on macOS")
def test_launchd_files_pass_plutil_lint(tmp_path: Path, config: Config, layout: DataLayout) -> None:
    be = launchd.LaunchdBackend(home=tmp_path / "home", runner=FakeRunner())
    files = be.render(config, layout, python=PY)
    base.write_files(files)
    res = subprocess.run(
        ["plutil", "-lint", *[str(f.path) for f in files]], capture_output=True, text=True
    )
    assert res.returncode == 0, res.stdout + res.stderr


def test_launchd_install_status_remove(tmp_path: Path, config: Config, layout: DataLayout) -> None:
    runner = FakeRunner({"launchctl print": (0, "state = waiting\n\tlast exit code = 3\n")})
    be = launchd.LaunchdBackend(home=tmp_path / "home", runner=runner)
    files = be.render(config, layout, python=PY)
    be.install(files)
    assert all(f.path.is_file() for f in files)
    assert layout.logs.is_dir()
    verbs = [c[1] for c in runner.calls]
    assert verbs.count("bootstrap") == 4 and verbs.count("bootout") == 4
    boot = next(c for c in runner.calls if c[1] == "bootstrap")
    assert boot[2].startswith("gui/") and boot[3].endswith(".plist")
    status = {s.job: s for s in be.status(config)}
    assert status["daily"].installed and status["daily"].last_exit_code == 3
    be.remove(config)
    assert not any(f.path.exists() for f in files)
    assert not any(s.installed for s in be.status(config))


def test_launchd_failed_bootstrap_raises(
    tmp_path: Path, config: Config, layout: DataLayout
) -> None:
    runner = FakeRunner({"launchctl bootstrap": (5, "")})
    be = launchd.LaunchdBackend(home=tmp_path / "home", runner=runner)
    with pytest.raises(base.SchedulerError):
        be.install(be.render(config, layout, python=PY))


# ---------------------------------------------------------------------------
# Task Scheduler
# ---------------------------------------------------------------------------


def _task(content: str) -> ET.Element:
    return ET.fromstring(content.encode("utf-16"))


def test_taskscheduler_xml_matches_appendix_c2(
    tmp_path: Path, config: Config, layout: DataLayout
) -> None:
    be = taskscheduler.TaskSchedulerBackend(home=tmp_path, runner=FakeRunner())
    files = {f.job: f for f in be.render(config, layout, python=PY)}
    daily = _task(files["daily"].content)
    assert daily.find(f"{NS}Settings/{NS}StartWhenAvailable").text == "true"  # type: ignore[union-attr]
    trig = daily.find(f"{NS}Triggers/{NS}CalendarTrigger")
    assert trig is not None
    assert trig.find(f"{NS}StartBoundary").text.endswith("T07:30:00")  # type: ignore[union-attr]
    assert trig.find(f"{NS}ScheduleByDay/{NS}DaysInterval").text == "1"  # type: ignore[union-attr]
    exe = daily.find(f"{NS}Actions/{NS}Exec")
    assert exe is not None
    assert exe.find(f"{NS}Command").text == PY  # type: ignore[union-attr]
    args = exe.find(f"{NS}Arguments").text or ""  # type: ignore[union-attr]
    assert args.startswith("-m kallimachos --data-dir ") and args.endswith(" run daily")
    assert f'"{layout.root}"' in args  # path with a space is quoted
    weekly = _task(files["weekly"].content)
    assert weekly.find(f".//{NS}DaysOfWeek/{NS}Sunday") is not None
    catchup = _task(files["catchup"].content)
    assert catchup.find(f"{NS}Triggers/{NS}LogonTrigger") is not None
    assert files["daily"].encoding == "utf-16"


def test_taskscheduler_install_writes_utf16_and_calls_schtasks(
    tmp_path: Path, config: Config, layout: DataLayout
) -> None:
    runner = FakeRunner()
    be = taskscheduler.TaskSchedulerBackend(home=tmp_path, runner=runner)
    files = be.render(config, layout, python=PY)
    be.install(files)
    raw = files[0].path.read_bytes()
    assert raw[:2] in (b"\xff\xfe", b"\xfe\xff")  # BOM
    creates = [c for c in runner.calls if c[:2] == ["schtasks", "/Create"]]
    assert len(creates) == 4
    assert creates[0][2:4] == ["/TN", "\\Kallimachos\\daily"] and "/XML" in creates[0]


def test_taskscheduler_status_parses_list_output(tmp_path: Path, config: Config) -> None:
    out = "TaskName: \\Kallimachos\\daily\nLast Run Time:  10/18/2026 7:40:00 AM\nLast Result: 0\n"
    runner = FakeRunner({"schtasks /Query": (0, out)})
    st = {s.job: s for s in taskscheduler.TaskSchedulerBackend(runner=runner).status(config)}
    assert st["daily"].installed and st["daily"].last_exit_code == 0
    assert st["daily"].last_run_at == "10/18/2026 7:40:00 AM"
    runner = FakeRunner({"schtasks /Query": (1, "")})
    assert not any(
        s.installed for s in taskscheduler.TaskSchedulerBackend(runner=runner).status(config)
    )


# ---------------------------------------------------------------------------
# systemd and cron
# ---------------------------------------------------------------------------


def test_systemd_units_match_appendix_c3(
    tmp_path: Path, config: Config, layout: DataLayout
) -> None:
    be = systemd.SystemdBackend(home=tmp_path / "home", runner=FakeRunner())
    files = {f.path.name: f for f in be.render(config, layout, python=PY)}
    unit_dir = tmp_path / "home" / ".config" / "systemd" / "user"
    assert all(f.path.parent == unit_dir for f in files.values())
    timer = files["kallimachos-daily.timer"].content
    assert "OnCalendar=*-*-* 07:30:00" in timer and "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer
    assert "OnCalendar=Sun *-*-* 09:30:00" in files["kallimachos-weekly.timer"].content
    service = files["kallimachos-daily.service"].content
    assert "Type=oneshot" in service
    root = systemd.unit_quote(str(layout.root))  # backslashes are doubled in unit quoting
    assert f'ExecStart="{PY}" -m kallimachos --data-dir {root} run daily' in service
    assert "kallimachos-catchup.timer" not in files
    assert "WantedBy=default.target" in files["kallimachos-catchup.service"].content
    assert systemd.unit_quote("50%") == "50%%"


def test_systemd_install_status_remove(tmp_path: Path, config: Config, layout: DataLayout) -> None:
    show = "ExecMainExitTimestamp=Sun 2026-10-18 07:31:02 UTC\nExecMainStatus=3\n"
    runner = FakeRunner({"systemctl --user": (0, show)})
    be = systemd.SystemdBackend(home=tmp_path / "home", runner=runner)
    be.install(be.render(config, layout, python=PY))
    enables = [c for c in runner.calls if "enable" in c]
    assert ["systemctl", "--user", "enable", "--now", "kallimachos-daily.timer"] in enables
    assert ["systemctl", "--user", "enable", "kallimachos-catchup.service"] in enables
    st = {s.job: s for s in be.status(config)}
    assert st["daily"].last_exit_code == 3 and st["daily"].last_run_at
    be.remove(config)
    assert not list(be.unit_dir.iterdir())


def test_cron_block_keeps_user_lines(tmp_path: Path, config: Config, layout: DataLayout) -> None:
    user = "MAILTO=someone@example.org\n0 1 * * * /usr/bin/true\n"
    runner = FakeRunner({"crontab -l": (0, user)})
    be = cron.CronBackend(home=tmp_path, runner=runner)
    files = be.render(config, layout, python=PY)
    lines = [f.content for f in files]
    assert lines[0].startswith("30 7 * * * KALLIMACHOS_HOME=")
    assert lines[1].startswith("30 9 * * 0 ")
    assert lines[3].startswith("@reboot ") and "schedule run-missed" in lines[3]
    be.install(files)
    written = files[0].path.read_text(encoding="utf-8")
    assert written.startswith(user.rstrip("\n").split("\n")[0])
    assert "0 1 * * * /usr/bin/true" in written and written.count(cron.END) == 1
    assert runner.calls[-1] == ["crontab", str(files[0].path)]
    again = cron.merge_block(written, [f.content.rstrip("\n") for f in files])
    assert again == written  # idempotent
    removed = cron.merge_block(written, [])
    assert "kallimachos" not in removed and "/usr/bin/true" in removed


# ---------------------------------------------------------------------------
# Hermes
# ---------------------------------------------------------------------------


def test_hermes_scripts_and_commands(tmp_path: Path, config: Config, layout: DataLayout) -> None:
    config.notify.channels = ["telegram"]
    be = hermes.HermesBackend(home=tmp_path / "home", runner=FakeRunner())
    files = {f.job: f for f in be.render(config, layout, python=PY)}
    daily = files["daily"]
    assert daily.path == tmp_path / "home" / ".hermes" / "scripts" / "kallimachos-daily.sh"
    assert daily.mode == 0o755
    assert daily.content.startswith("#!/bin/sh\n")
    assert "--json run daily --no-notify" in daily.content
    assert "--json" not in files["archive"].content and "archive run" in files["archive"].content
    cmds = be.cron_commands(config)
    names = [c[c.index("--name") + 1] for c in cmds]
    assert names == ["kallimachos-daily", "kallimachos-weekly", "kallimachos-archive"]
    daily_cmd = cmds[0]
    assert daily_cmd[:4] == ["hermes", "cron", "create", "30 7 * * *"]
    assert daily_cmd[daily_cmd.index("--skill") + 1] == "kallimachos-run"
    assert daily_cmd[daily_cmd.index("--script") + 1] == "kallimachos-daily.sh"
    assert daily_cmd[daily_cmd.index("--deliver") + 1] == "telegram"
    assert "data, not instructions" in daily_cmd[4]
    assert cmds[1][3] == "30 9 * * 0"
    assert "--no-agent" in cmds[2] and "--skill" not in cmds[2]
    be.install(list(files.values()))
    if os.name != "nt":  # Windows has no POSIX modes
        assert daily.path.stat().st_mode & 0o777 == 0o755
    text = "\n".join(be.guidance(config, layout, python=PY))
    assert "hermes skills install" in text and "jobs.json" in text


def test_hermes_status_reads_jobs_json_without_writing(tmp_path: Path, config: Config) -> None:
    be = hermes.HermesBackend(home=tmp_path / "home", runner=FakeRunner())
    jobs = tmp_path / "home" / ".hermes" / "cron" / "jobs.json"
    jobs.parent.mkdir(parents=True)
    payload = (
        '{"jobs": [{"name": "kallimachos-daily", "last_run_at": "2026-10-18T07:30:00Z",'
        ' "last_status": "ok"}]}'
    )
    jobs.write_text(payload, encoding="utf-8")
    be.script_path("daily").parent.mkdir(parents=True)
    be.script_path("daily").write_text("#!/bin/sh\n", encoding="utf-8")
    st = {s.job: s for s in be.status(config)}
    assert st["daily"].installed and st["daily"].last_run_at == "2026-10-18T07:30:00Z"
    assert jobs.read_text(encoding="utf-8") == payload


# ---------------------------------------------------------------------------
# GitHub Actions
# ---------------------------------------------------------------------------


def test_github_cron_is_converted_to_utc() -> None:
    zone = __import__("zoneinfo").ZoneInfo("Europe/Istanbul")  # UTC+3, no DST
    today = _dt.date(2026, 10, 14)  # a Wednesday
    assert gh_actions.utc_cron("07:30", zone, None, today) == "30 4 * * *"
    assert gh_actions.utc_cron("09:30", zone, "sun", today) == "30 6 * * 0"
    assert gh_actions.utc_cron("01:15", zone, "mon", today) == "15 22 * * 0"  # previous UTC day


def test_github_workflow_is_valid_and_documented(tmp_path: Path) -> None:
    cfg = Config(timezone="Europe/Istanbul")
    text = gh_actions.workflow_yaml(cfg, today=_dt.date(2026, 10, 14))
    data = yaml.safe_load(text)
    on = data[True] if True in data else data["on"]  # YAML 1.1 reads `on` as a boolean
    assert [c["cron"] for c in on["schedule"]] == ["30 4 * * *", "30 6 * * 0"]
    assert "workflow_dispatch" in on
    steps = data["jobs"]["scan"]["steps"]
    run_lines = "\n".join(str(s.get("run", "")) for s in steps)
    assert "secrets.KALLIMACHOS_CONFIG_B64" in text and "secrets.KALLIMACHOS_PROFILE_B64" in text
    assert "kallimachos run" in run_lines
    assert data["permissions"] == {"contents": "read"}
    be = gh_actions.GitHubActionsBackend(repo_dir=tmp_path / "repo")
    guidance = " ".join(be.guidance(cfg, DataLayout(tmp_path), python=PY))
    assert "PRIVATE" in guidance and "60 days" in guidance and "default branch" in guidance


def test_github_install_never_overwrites_foreign_file(tmp_path: Path) -> None:
    cfg = Config(timezone="UTC")
    be = gh_actions.GitHubActionsBackend(repo_dir=tmp_path / "repo")
    files = be.render(cfg, DataLayout(tmp_path), python=PY)
    be.install(files)
    assert be.status(cfg)[0].installed
    be.workflow_path.write_text("name: mine\n", encoding="utf-8")
    with pytest.raises(UsageError):
        be.install(files)
    be.remove(cfg)
    assert be.workflow_path.read_text(encoding="utf-8") == "name: mine\n"


def test_repository_example_workflow_is_current(repo_root: Path) -> None:
    """``.github/workflows/scan.yml.example`` is the rendering for the default config in UTC."""
    example = (repo_root / gh_actions.EXAMPLE_PATH).read_text(encoding="utf-8")
    expected = gh_actions.workflow_yaml(Config(timezone="UTC"), today=_dt.date(2026, 1, 7))
    assert example == expected
