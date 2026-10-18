"""macOS ``~/Library/LaunchAgents/io.github.oselcukk.kallimachos.<job>.plist``.

``StartCalendarInterval`` jobs missed while the Mac sleeps run once on wake and several missed
intervals coalesce into one event (``man launchd.plist``). Behaviour for a powered-off machine is
not documented, so the ``catchup`` agent (``RunAtLoad true``) runs ``schedule run-missed`` at login.
"""

from __future__ import annotations

import os
import plistlib
import re
import sys
from pathlib import Path
from typing import Any

from kallimachos.config.schema import Config
from kallimachos.paths import DataLayout
from kallimachos.schedule.base import (
    JOBS,
    LABEL_PREFIX,
    WEEKDAY_NUMBER,
    JobSpec,
    JobStatus,
    Runner,
    ScheduleFile,
    check,
    default_runner,
    job_specs,
    kallimachos_argv,
    mtime_iso,
    resolve_home,
    write_files,
)

_LAST_EXIT = re.compile(r"last exit code = (-?\d+)")


def label(job: str) -> str:
    return f"{LABEL_PREFIX}.{job}"


def _uid() -> int:
    if sys.platform == "win32":
        return 0
    return os.getuid()


def plist_dict(spec: JobSpec, layout: DataLayout, python: str) -> dict[str, Any]:
    logs = layout.logs
    data: dict[str, Any] = {
        "Label": label(spec.job),
        "ProgramArguments": kallimachos_argv(python, layout, spec),
    }
    if spec.at_login:
        data["RunAtLoad"] = True
    else:
        interval: dict[str, int] = {"Hour": spec.hour, "Minute": spec.minute}
        if spec.weekday is not None:
            interval["Weekday"] = WEEKDAY_NUMBER[spec.weekday]
        data["StartCalendarInterval"] = interval
        data["RunAtLoad"] = False
    data.update(
        {
            "ProcessType": "Background",
            "LowPriorityIO": True,
            "StandardOutPath": str(logs / f"launchd-{spec.job}.out"),
            "StandardErrorPath": str(logs / f"launchd-{spec.job}.err"),
            "EnvironmentVariables": {"KALLIMACHOS_HOME": str(layout.root)},
        }
    )
    return data


class LaunchdBackend:
    name = "launchd"

    def __init__(self, *, home: Path | None = None, runner: Runner | None = None) -> None:
        self.home = resolve_home(home)
        self.runner = runner or default_runner

    @property
    def agents_dir(self) -> Path:
        return self.home / "Library" / "LaunchAgents"

    def plist_path(self, job: str) -> Path:
        return self.agents_dir / f"{label(job)}.plist"

    def render(self, config: Config, layout: DataLayout, *, python: str) -> list[ScheduleFile]:
        out: list[ScheduleFile] = []
        for spec in job_specs(config):
            content = plistlib.dumps(plist_dict(spec, layout, python), sort_keys=False).decode()
            out.append(ScheduleFile(self.plist_path(spec.job), content, spec.job))
        return out

    def install(self, files: list[ScheduleFile]) -> None:
        if files:
            # launchd opens StandardOutPath itself; the directory must exist
            log_dir = _log_dir(files[0])
            if log_dir is not None:
                log_dir.mkdir(parents=True, exist_ok=True)
        write_files(files)
        domain = f"gui/{_uid()}"
        for f in files:
            self.runner(["launchctl", "bootout", f"{domain}/{label(f.job)}"])  # may not be loaded
            check(self.runner(["launchctl", "bootstrap", domain, str(f.path)]), "launchctl")

    def remove(self, config: Config) -> None:
        domain = f"gui/{_uid()}"
        for job in JOBS:
            path = self.plist_path(job)
            if path.exists():
                self.runner(["launchctl", "bootout", f"{domain}/{label(job)}"])
                path.unlink()

    def status(self, config: Config) -> list[JobStatus]:
        out: list[JobStatus] = []
        domain = f"gui/{_uid()}"
        for job in JOBS:
            path = self.plist_path(job)
            if not path.exists():
                out.append(JobStatus(job, False))
                continue
            st = JobStatus(job, True, detail=str(path))
            res = self.runner(["launchctl", "print", f"{domain}/{label(job)}"])
            m = _LAST_EXIT.search(res.stdout or "")
            if m:
                st.last_exit_code = int(m.group(1))
            elif res.returncode != 0:
                st.detail += " (not loaded)"
            try:
                info = plistlib.loads(path.read_bytes())
                log = Path(str(info.get("StandardOutPath", "")))
                if log.is_file():
                    st.last_run_at = mtime_iso(log)
            except (OSError, plistlib.InvalidFileException):
                pass
            out.append(st)
        return out

    def guidance(self, config: Config, layout: DataLayout, *, python: str) -> list[str]:
        return [
            "launchd runs a job missed during sleep once on wake (several missed runs coalesce).",
            "A powered-off Mac is covered by the catch-up agent: at login it runs "
            "`kallimachos schedule run-missed` (at most schedule.max_catch_up_days days back).",
            f"Remove with `kallimachos schedule remove`; files live in {self.agents_dir}.",
        ]


def _log_dir(f: ScheduleFile) -> Path | None:
    try:
        info = plistlib.loads(f.content.encode())
    except plistlib.InvalidFileException:
        return None
    out = info.get("StandardOutPath")
    return Path(str(out)).parent if out else None
