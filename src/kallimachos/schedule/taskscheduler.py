"""Windows ``schtasks /Create /XML`` with StartWhenAvailable.

``StartWhenAvailable`` applies to time-based tasks: a missed start is queued and runs after the
default ~10 minute delay (Microsoft task schema documentation). The ``catchup`` task has a
``LogonTrigger`` and runs ``schedule run-missed``. The XML files are kept in
``<data_dir>/schedule/taskscheduler/`` (UTF-16, as Task Scheduler exports them) and registered under
the ``\\Kallimachos\\`` task folder.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape

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
    resolve_home,
    write_files,
)

TASK_FOLDER = "\\Kallimachos\\"
START_DATE = "2026-01-01"

_LAST_RUN = re.compile(r"^\s*Last Run Time:\s*(.+?)\s*$", re.M)
_LAST_RESULT = re.compile(r"^\s*Last Result:\s*(-?\d+)\s*$", re.M)


def task_name(job: str) -> str:
    return f"{TASK_FOLDER}{job}"


def windowless_python(python: str) -> str:
    """``pythonw.exe`` next to ``python.exe`` avoids a console window popping up at 07:30."""
    path = Path(python)
    if path.name.lower() == "python.exe":
        candidate = path.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return python


def _trigger(spec: JobSpec) -> str:
    if spec.at_login:
        user = os.environ.get("USERNAME")
        domain = os.environ.get("USERDOMAIN")
        who = f"{domain}\\{user}" if domain and user else (user or "")
        uid = f"<UserId>{escape(who)}</UserId>" if who else ""
        return f"<LogonTrigger><Enabled>true</Enabled>{uid}<Delay>PT2M</Delay></LogonTrigger>"
    start = f"<StartBoundary>{START_DATE}T{spec.hour:02d}:{spec.minute:02d}:00</StartBoundary>"
    if spec.weekday is not None:
        day = WEEKDAY_NAME[spec.weekday]
        sched = (
            "<ScheduleByWeek><WeeksInterval>1</WeeksInterval>"
            f"<DaysOfWeek><{day} /></DaysOfWeek></ScheduleByWeek>"
        )
    else:
        sched = "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
    return f"<CalendarTrigger>{start}<Enabled>true</Enabled>{sched}</CalendarTrigger>"


def task_xml(spec: JobSpec, layout: DataLayout, python: str) -> str:
    argv = kallimachos_argv(windowless_python(python), layout, spec)
    command = escape(argv[0])
    arguments = escape(subprocess.list2cmdline(argv[1:]))
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        f"  <RegistrationInfo><Description>{escape(spec.description)}</Description>"
        "</RegistrationInfo>\n"
        f"  <Triggers>{_trigger(spec)}</Triggers>\n"
        '  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType>'
        "<RunLevel>LeastPrivilege</RunLevel></Principal></Principals>\n"
        "  <Settings><StartWhenAvailable>true</StartWhenAvailable>"
        "<RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>"
        "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>"
        "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
        "<ExecutionTimeLimit>PT1H</ExecutionTimeLimit><Enabled>true</Enabled></Settings>\n"
        f'  <Actions Context="Author"><Exec><Command>{command}</Command>'
        f"<Arguments>{arguments}</Arguments></Exec></Actions>\n"
        "</Task>\n"
    )


class TaskSchedulerBackend:
    name = "taskscheduler"

    def __init__(self, *, home: Path | None = None, runner: Runner | None = None) -> None:
        self.home = resolve_home(home)
        self.runner = runner or default_runner

    @staticmethod
    def xml_dir(layout: DataLayout) -> Path:
        return layout.root / "schedule" / "taskscheduler"

    def render(self, config: Config, layout: DataLayout, *, python: str) -> list[ScheduleFile]:
        folder = self.xml_dir(layout)
        return [
            ScheduleFile(
                folder / f"kallimachos-{spec.job}.xml",
                task_xml(spec, layout, python),
                spec.job,
                encoding="utf-16",
            )
            for spec in job_specs(config)
        ]

    def install(self, files: list[ScheduleFile]) -> None:
        write_files(files)
        for f in files:
            argv = ["schtasks", "/Create", "/TN", task_name(f.job), "/XML", str(f.path), "/F"]
            check(self.runner(argv), "schtasks /Create")

    def remove(self, config: Config) -> None:
        for job in JOBS:
            res = self.runner(["schtasks", "/Query", "/TN", task_name(job)])
            if res.returncode == 0:
                check(
                    self.runner(["schtasks", "/Delete", "/TN", task_name(job), "/F"]),
                    "schtasks /Delete",
                )

    def status(self, config: Config) -> list[JobStatus]:
        out: list[JobStatus] = []
        for job in JOBS:
            res = self.runner(["schtasks", "/Query", "/TN", task_name(job), "/V", "/FO", "LIST"])
            if res.returncode != 0:
                out.append(JobStatus(job, False))
                continue
            st = JobStatus(job, True, detail=task_name(job))
            text = res.stdout or ""
            if m := _LAST_RUN.search(text):
                st.last_run_at = m.group(1)
            if m := _LAST_RESULT.search(text):
                st.last_exit_code = int(m.group(1))
            out.append(st)
        return out

    def guidance(self, config: Config, layout: DataLayout, *, python: str) -> list[str]:
        return [
            "Task Scheduler starts a missed task when the computer is available again "
            "(StartWhenAvailable; queued with a ~10 minute delay).",
            "The catch-up task runs `kallimachos schedule run-missed` two minutes after logon.",
            f"Tasks are registered under {TASK_FOLDER}; see them in taskschd.msc. "
            "Remove with `kallimachos schedule remove`.",
        ]
