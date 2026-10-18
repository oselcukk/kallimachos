"""``doctor`` checks for scheduling.

``kallimachos.doctor.run_doctor`` appends ``schedule.doctor_checks(layout, config)``. Every check is
read-only: it looks at the installed files, asks the scheduler for the last exit code and compares
the last successful run with the configured times.
"""

from __future__ import annotations

import datetime as _dt

from kallimachos.config.schema import Config
from kallimachos.doctor import DoctorCheck
from kallimachos.interfaces import KallimachosError, RunKind
from kallimachos.paths import DataLayout
from kallimachos.schedule.base import Backend, get_backend, job_specs
from kallimachos.state import runs as state_runs

STALE_AFTER_DAYS = 2


def doctor_checks(
    layout: DataLayout,
    config: Config | None,
    *,
    backend: Backend | None = None,
    today: _dt.date | None = None,
) -> list[DoctorCheck]:
    if config is None:
        return []
    if config.schedule.backend == "none" and backend is None:
        return [DoctorCheck("schedule", True, "no scheduler configured (schedule.backend: none)")]
    checks: list[DoctorCheck] = []
    if "email" in config.notify.channels and config.notify.email.confirmed_at is None:
        checks.append(
            DoctorCheck(
                "schedule e-mail",
                False,
                "e-mail channel not confirmed; `schedule install` refuses until "
                "`notify test --channel email` + `notify confirm-email`",
            )
        )
    try:
        be = backend or get_backend(config)
        statuses = {s.job: s for s in be.status(config)}
    except (KallimachosError, OSError) as exc:
        return [*checks, DoctorCheck("schedule", False, f"status unavailable: {exc}")]
    wanted = [s.job for s in job_specs(config)]
    if be.name == "gh_actions":
        wanted = ["daily", "weekly"]
    missing = [job for job in wanted if not statuses.get(job) or not statuses[job].installed]
    if len(missing) == len(wanted):
        checks.append(
            DoctorCheck(
                "schedule",
                False,
                f"{be.name}: not installed; run `kallimachos schedule install` "
                "(shows the files and asks first)",
            )
        )
        return checks
    if missing:
        checks.append(
            DoctorCheck(
                "schedule",
                False,
                f"{be.name}: missing job(s) {', '.join(missing)}; rerun `kallimachos schedule "
                "install`",
            )
        )
    else:
        checks.append(DoctorCheck("schedule", True, f"{be.name}: {', '.join(wanted)} installed"))
    failing = [
        f"{job} exit {st.last_exit_code}"
        for job, st in statuses.items()
        if st.installed and st.last_exit_code not in (None, 0)
    ]
    if failing:
        checks.append(
            DoctorCheck(
                "schedule last exit",
                False,
                "; ".join(failing) + " (see the exit-code table in the doctor skill)",
            )
        )
    today = today or _dt.date.today()
    last = state_runs.last_successful_run_date(layout, RunKind.DAILY)
    if last is None:
        checks.append(DoctorCheck("schedule runs", True, "no successful daily run yet"))
    elif (today - last).days > STALE_AFTER_DAYS:
        checks.append(
            DoctorCheck(
                "schedule runs",
                False,
                f"last successful daily run {last.isoformat()} ({(today - last).days} days ago); "
                "run `kallimachos schedule run-missed` and check `kallimachos schedule status`",
            )
        )
    else:
        checks.append(
            DoctorCheck("schedule runs", True, f"last successful daily run {last.isoformat()}")
        )
    return checks
