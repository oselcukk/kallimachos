"""crontab lines plus ``@reboot schedule run-missed`` when systemd is absent.

Only the block between ``# BEGIN kallimachos`` and ``# END kallimachos`` is ever changed; every
other crontab line stays as it is. The merged crontab is written to
``<data_dir>/schedule/crontab.txt`` and installed with ``crontab FILE``.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from kallimachos.config.schema import Config
from kallimachos.paths import DataLayout
from kallimachos.schedule.base import (
    JOBS,
    WEEKDAY_NUMBER,
    JobSpec,
    JobStatus,
    Runner,
    ScheduleFile,
    check,
    default_runner,
    job_specs,
    kallimachos_argv,
    resolve_home,
)

BEGIN = "# BEGIN kallimachos (managed by `kallimachos schedule`; edit with that command)"
END = "# END kallimachos"


def cron_line(spec: JobSpec, layout: DataLayout, python: str) -> str:
    argv = kallimachos_argv(python, layout, spec)
    command = " ".join(shlex.quote(a) for a in argv).replace("%", "\\%")
    env = "KALLIMACHOS_HOME=" + shlex.quote(str(layout.root)).replace("%", "\\%")
    if spec.at_login:
        when = "@reboot"
    else:
        dow = str(WEEKDAY_NUMBER[spec.weekday]) if spec.weekday else "*"
        when = f"{spec.minute} {spec.hour} * * {dow}"
    return f"{when} {env} {command}  # kallimachos-{spec.job}"


def merge_block(current: str, block_lines: list[str]) -> str:
    """Replace (or append, or with an empty list remove) the managed block."""
    lines = current.splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if line.startswith("# BEGIN kallimachos"):
            inside = True
            continue
        if inside and line.startswith(END):
            inside = False
            continue
        if not inside:
            out.append(line)
    while out and not out[-1].strip():
        out.pop()
    if block_lines:
        if out:
            out.append("")
        out += [BEGIN, *block_lines, END]
    return "\n".join(out) + ("\n" if out else "")


class CronBackend:
    name = "cron"

    def __init__(self, *, home: Path | None = None, runner: Runner | None = None) -> None:
        self.home = resolve_home(home)
        self.runner = runner or default_runner

    def render(self, config: Config, layout: DataLayout, *, python: str) -> list[ScheduleFile]:
        path = layout.root / "schedule" / "crontab.txt"
        return [
            ScheduleFile(path, cron_line(spec, layout, python) + "\n", spec.job)
            for spec in job_specs(config)
        ]

    def _current(self) -> str:
        res = self.runner(["crontab", "-l"])
        return res.stdout if res.returncode == 0 else ""  # "no crontab for user" -> empty

    def _write(self, text: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        check(self.runner(["crontab", str(target)]), "crontab")

    def install(self, files: list[ScheduleFile]) -> None:
        if not files:
            return
        block = [f.content.rstrip("\n") for f in files]
        merged = merge_block(self._current(), block)
        self._write(merged, files[0].path)

    def remove(self, config: Config) -> None:
        current = self._current()
        if BEGIN.split(" (")[0] not in current:
            return
        from kallimachos.paths import data_dir

        target = data_dir(config.data_dir) / "schedule" / "crontab.txt"
        self._write(merge_block(current, []), target)

    def status(self, config: Config) -> list[JobStatus]:
        current = self._current()
        return [JobStatus(job, f"# kallimachos-{job}" in current, detail="crontab") for job in JOBS]

    def guidance(self, config: Config, layout: DataLayout, *, python: str) -> list[str]:
        return [
            "cron skips runs while the machine is off; the @reboot line runs "
            "`kallimachos schedule run-missed` after boot.",
            "cron uses the machine's local time. Remove with `kallimachos schedule remove`.",
        ]
