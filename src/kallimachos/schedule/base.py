"""Scheduler back-end protocol and shared helpers.

Every back-end renders its files first (``render``); the CLI shows them and asks for approval, and
only then ``install`` writes them and calls the system tool. System commands always go through an
injectable ``Runner`` so tests never touch the real launchd / Task Scheduler / systemd / crontab.
"""

from __future__ import annotations

import datetime as _dt
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from kallimachos.config.schema import Config
from kallimachos.interfaces import UsageError
from kallimachos.paths import DataLayout

JobName = Literal["daily", "weekly", "archive", "catchup"]
JOBS: tuple[JobName, ...] = ("daily", "weekly", "archive", "catchup")

LABEL_PREFIX = "io.github.oselcukk.kallimachos"
"""Reverse-DNS prefix of launchd labels."""

WEEKDAY_NUMBER = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
"""cron / launchd day numbers (0 = Sunday)."""

WEEKDAY_NAME = {
    "sun": "Sunday",
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
}

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a system command without a shell; never raises on a non-zero exit."""
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))


@dataclass
class ScheduleFile:
    path: Path
    content: str
    job: JobName
    encoding: str = "utf-8"
    mode: int | None = None  # chmod after writing (Hermes scripts)


@dataclass
class JobStatus:
    job: JobName
    installed: bool
    last_run_at: str | None = None
    last_exit_code: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class JobSpec:
    """One scheduled job: what it runs and when (local time of the machine)."""

    job: JobName
    args: tuple[str, ...]  # arguments after ``python -m kallimachos --data-dir DIR``
    hour: int = 0
    minute: int = 0
    weekday: str | None = None  # mon..sun, weekly jobs only
    at_login: bool = False  # catch-up job: run at load / logon / boot
    description: str = ""


class Backend(Protocol):
    name: str

    def render(self, config: Config, layout: DataLayout, *, python: str) -> list[ScheduleFile]: ...

    def install(self, files: list[ScheduleFile]) -> None: ...

    def remove(self, config: Config) -> None: ...

    def status(self, config: Config) -> list[JobStatus]: ...

    def guidance(self, config: Config, layout: DataLayout, *, python: str) -> list[str]: ...


class SchedulerError(UsageError):
    """A system scheduler command failed (exit 2 with the tool's message)."""


def parse_hhmm(value: str) -> tuple[int, int]:
    """``"07:30"`` -> ``(7, 30)``; rejects hours above 23."""
    try:
        hh, mm = value.split(":")
        hour, minute = int(hh), int(mm)
    except ValueError:
        raise UsageError(f"invalid time {value!r}; expected HH:MM") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise UsageError(f"invalid time {value!r}; expected HH:MM between 00:00 and 23:59")
    return hour, minute


def job_specs(config: Config) -> list[JobSpec]:
    """The jobs a configuration asks for. ``archive`` only with the archive module on (it runs
    before the daily scan); the catch-up job only with
    ``schedule.catch_up_missed``."""
    sched = config.schedule
    dh, dm = parse_hhmm(sched.daily_at)
    wh, wm = parse_hhmm(sched.weekly_at)
    specs = [
        JobSpec("daily", ("run", "daily"), dh, dm, description="Kallimachos daily literature scan"),
        JobSpec(
            "weekly",
            ("run", "weekly"),
            wh,
            wm,
            weekday=sched.weekly_on,
            description="Kallimachos weekly network scan",
        ),
    ]
    if config.archive.enabled:
        ah, am = parse_hhmm(sched.archive_at)
        specs.append(
            JobSpec("archive", ("archive", "run"), ah, am, description="Kallimachos archive round")
        )
    if sched.catch_up_missed:
        specs.append(
            JobSpec(
                "catchup",
                ("schedule", "run-missed"),
                at_login=True,
                description="Kallimachos catch-up of missed days",
            )
        )
    return specs


def kallimachos_argv(python: str, layout: DataLayout, spec: JobSpec) -> list[str]:
    """``python -m kallimachos --data-dir <root> <args>``; the data directory is explicit so the
    job never depends on the scheduler's environment."""
    return [python, "-m", "kallimachos", "--data-dir", str(layout.root), *spec.args]


def default_python() -> str:
    """The interpreter that runs this installation (the venv's python when installed in one)."""
    return sys.executable or shutil.which("python3") or "python3"


def write_files(files: Sequence[ScheduleFile]) -> None:
    for f in files:
        f.path.parent.mkdir(parents=True, exist_ok=True)
        f.path.write_text(f.content, encoding=f.encoding, newline="\n")
        if f.mode is not None:
            os.chmod(f.path, f.mode)


def check(result: subprocess.CompletedProcess[str], what: str) -> None:
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip().splitlines()
        raise SchedulerError(f"{what} failed (exit {result.returncode}): {msg[0] if msg else ''}")


def local_backend_name(platform: str | None = None, *, has_systemd: bool | None = None) -> str:
    """``local`` resolves to launchd (macOS), taskscheduler (Windows), systemd or cron (Linux)."""
    platform = platform or sys.platform
    if platform == "darwin":
        return "launchd"
    if platform.startswith("win"):
        return "taskscheduler"
    if has_systemd is None:
        has_systemd = Path("/run/systemd/system").is_dir() and shutil.which("systemctl") is not None
    return "systemd" if has_systemd else "cron"


def get_backend(
    config: Config,
    *,
    backend: str | None = None,
    home: Path | None = None,
    runner: Runner | None = None,
    platform: str | None = None,
    repo_dir: Path | None = None,
) -> Backend:
    """local -> launchd / taskscheduler / systemd (cron fallback); hermes; github. ``backend``
    overrides ``schedule.backend`` (``--backend``); the platform names are accepted too."""
    from kallimachos.schedule.cron import CronBackend
    from kallimachos.schedule.gh_actions import GitHubActionsBackend
    from kallimachos.schedule.hermes import HermesBackend
    from kallimachos.schedule.launchd import LaunchdBackend
    from kallimachos.schedule.systemd import SystemdBackend
    from kallimachos.schedule.taskscheduler import TaskSchedulerBackend

    choice = backend or config.schedule.backend
    if choice == "none":
        raise UsageError("schedule.backend is 'none'; choose local, hermes or github")
    if choice == "local":
        choice = local_backend_name(platform)
    if choice == "launchd":
        return LaunchdBackend(home=home, runner=runner)
    if choice == "taskscheduler":
        return TaskSchedulerBackend(home=home, runner=runner)
    if choice == "systemd":
        return SystemdBackend(home=home, runner=runner)
    if choice == "cron":
        return CronBackend(home=home, runner=runner)
    if choice == "hermes":
        return HermesBackend(home=home, runner=runner)
    if choice in ("github", "gh_actions"):
        return GitHubActionsBackend(home=home, runner=runner, repo_dir=repo_dir)
    raise UsageError(f"unknown schedule backend {choice!r}")


def resolve_home(home: Path | None) -> Path:
    return home if home is not None else Path.home()


def mtime_iso(path: Path) -> str | None:
    """Modification time of a log file as an ISO timestamp (UTC), or None."""
    try:
        ts = path.stat().st_mtime
    except OSError:
        return None
    return _dt.datetime.fromtimestamp(ts, tz=_dt.UTC).isoformat(timespec="seconds")
