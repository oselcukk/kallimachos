"""Linux user timers ``kallimachos-<job>.timer`` with Persistent=true.

``Persistent=true`` stores the last trigger time on disk; when the timer is activated again it
fires immediately if a run was missed while it was inactive (``systemd.timer(5)``). The catch-up
job is a plain service wanted by ``default.target``: it runs ``schedule run-missed`` when the user
session starts. Timers of a user who is not logged in only run with ``loginctl enable-linger``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from kallimachos.config.schema import Config
from kallimachos.paths import DataLayout
from kallimachos.schedule.base import (
    JOBS,
    WEEKDAY_NAME,
    JobSpec,
    JobStatus,
    Runner,
    ScheduleFile,
    check,
    default_runner,
    job_specs,
    kallimachos_argv,
    write_files,
)

_SHOW = re.compile(r"^(\w+)=(.*)$", re.M)


def unit_quote(arg: str) -> str:
    """Quote one ExecStart argument (systemd.service "Command lines"); ``%`` is a specifier."""
    arg = arg.replace("%", "%%")
    if arg and not re.search(r'[\s"\'\\;]', arg):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def on_calendar(spec: JobSpec) -> str:
    day = WEEKDAY_NAME[spec.weekday][:3] + " " if spec.weekday else ""
    return f"{day}*-*-* {spec.hour:02d}:{spec.minute:02d}:00"


def service_unit(spec: JobSpec, layout: DataLayout, python: str) -> str:
    argv = " ".join(unit_quote(a) for a in kallimachos_argv(python, layout, spec))
    lines = [
        "[Unit]",
        f"Description={spec.description}",
        "",
        "[Service]",
        "Type=oneshot",
        f"Environment={unit_quote('KALLIMACHOS_HOME=' + str(layout.root))}",
        f"ExecStart={argv}",
    ]
    if spec.at_login:
        lines += ["", "[Install]", "WantedBy=default.target"]
    return "\n".join(lines) + "\n"


def timer_unit(spec: JobSpec) -> str:
    return (
        "[Unit]\n"
        f"Description={spec.description} (timer)\n"
        "\n"
        "[Timer]\n"
        f"OnCalendar={on_calendar(spec)}\n"
        "Persistent=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


class SystemdBackend:
    name = "systemd"

    def __init__(self, *, home: Path | None = None, runner: Runner | None = None) -> None:
        if home is None:
            xdg = os.environ.get("XDG_CONFIG_HOME")
            self.config_home = Path(xdg) if xdg else Path.home() / ".config"
        else:
            self.config_home = home / ".config"
        self.runner = runner or default_runner

    @property
    def unit_dir(self) -> Path:
        return self.config_home / "systemd" / "user"

    def render(self, config: Config, layout: DataLayout, *, python: str) -> list[ScheduleFile]:
        out: list[ScheduleFile] = []
        for spec in job_specs(config):
            base = self.unit_dir / f"kallimachos-{spec.job}"
            out.append(
                ScheduleFile(
                    base.with_suffix(".service"), service_unit(spec, layout, python), spec.job
                )
            )
            if not spec.at_login:
                out.append(ScheduleFile(base.with_suffix(".timer"), timer_unit(spec), spec.job))
        return out

    def _ctl(self, *args: str) -> list[str]:
        return ["systemctl", "--user", *args]

    def install(self, files: list[ScheduleFile]) -> None:
        write_files(files)
        check(self.runner(self._ctl("daemon-reload")), "systemctl --user daemon-reload")
        for f in files:
            if f.path.suffix == ".timer":
                check(self.runner(self._ctl("enable", "--now", f.path.name)), "systemctl enable")
            elif f.job == "catchup":
                check(self.runner(self._ctl("enable", f.path.name)), "systemctl enable")

    def remove(self, config: Config) -> None:
        found = False
        for job in JOBS:
            for suffix in (".timer", ".service"):
                path = self.unit_dir / f"kallimachos-{job}{suffix}"
                if path.exists():
                    found = True
                    if suffix == ".timer" or job == "catchup":
                        self.runner(self._ctl("disable", "--now", path.name))
                    path.unlink()
        if found:
            self.runner(self._ctl("daemon-reload"))

    def status(self, config: Config) -> list[JobStatus]:
        out: list[JobStatus] = []
        for job in JOBS:
            service = self.unit_dir / f"kallimachos-{job}.service"
            if not service.exists():
                out.append(JobStatus(job, False))
                continue
            st = JobStatus(job, True, detail=str(service))
            res = self.runner(
                self._ctl(
                    "show", service.name, "-p", "ExecMainExitTimestamp", "-p", "ExecMainStatus"
                )
            )
            props = dict(_SHOW.findall(res.stdout or ""))
            if props.get("ExecMainExitTimestamp"):
                st.last_run_at = props["ExecMainExitTimestamp"]
                if props.get("ExecMainStatus", "").lstrip("-").isdigit():
                    st.last_exit_code = int(props["ExecMainStatus"])
            out.append(st)
        return out

    def guidance(self, config: Config, layout: DataLayout, *, python: str) -> list[str]:
        return [
            "Timers use Persistent=true: a run missed while the machine was off fires once at the "
            "next boot or login.",
            "The catch-up service runs `kallimachos schedule run-missed` when your user session "
            "starts.",
            "To run while you are logged out, enable lingering once: `loginctl enable-linger`.",
            f"Units live in {self.unit_dir}; remove with `kallimachos schedule remove`.",
        ]
