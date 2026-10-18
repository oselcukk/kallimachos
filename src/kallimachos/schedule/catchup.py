"""``schedule run-missed``: at most ``max_catch_up_days`` back, one merged message.

The catch-up job (launchd ``RunAtLoad``, Task Scheduler logon trigger, systemd user service,
``@reboot``) calls ``run-missed``. Instead of one message per missed day it plans **one** run whose
window covers every missed day, so the user gets a single merged message.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from kallimachos.config.schema import Config
from kallimachos.interfaces import RunKind
from kallimachos.paths import DataLayout
from kallimachos.schedule.base import WEEKDAY_NUMBER, parse_hhmm
from kallimachos.state import runs as state_runs

WEEKLY_WINDOW_DAYS = 7


def missed_days(last_success: _dt.date | None, today: _dt.date, *, max_days: int) -> list[_dt.date]:
    """Days after ``last_success`` up to ``today`` (exclusive), capped at ``max_days``.

    The cap keeps the most recent days. Without any successful run there is nothing to catch up
    (the first run is ``run daily --first-run``, done once by the user)."""
    if last_success is None or max_days <= 0:
        return []
    start = max(last_success + _dt.timedelta(days=1), today - _dt.timedelta(days=max_days))
    return [start + _dt.timedelta(days=i) for i in range((today - start).days)]


@dataclass
class CatchUpPlan:
    kind: RunKind
    day: _dt.date  # the run date (run id); its window ends at the end of this day or now
    window_days: int
    missed: list[_dt.date]


def _due_today(now: _dt.datetime, hhmm: str) -> bool:
    hour, minute = parse_hhmm(hhmm)
    return (now.hour, now.minute) >= (hour, minute)


def last_weekly_slot(now: _dt.datetime, weekday: str, hhmm: str) -> _dt.date:
    """The most recent date (today included once its time has passed) of the weekly job."""
    target = (WEEKDAY_NUMBER[weekday] - 1) % 7  # Sunday=0 -> Python Monday=0 numbering
    back = (now.date().weekday() - target) % 7
    day = now.date() - _dt.timedelta(days=back)
    if back == 0 and not _due_today(now, hhmm):
        day -= _dt.timedelta(days=7)
    return day


def plan(layout: DataLayout, config: Config, now: _dt.datetime) -> list[CatchUpPlan]:
    """What ``run-missed`` would run now. Nothing when the last run is current, when no run ever
    succeeded, or when ``schedule.catch_up_missed`` is off."""
    sched = config.schedule
    if not sched.catch_up_missed:
        return []
    out: list[CatchUpPlan] = []

    today = now.date()
    target = today if _due_today(now, sched.daily_at) else today - _dt.timedelta(days=1)
    last = state_runs.last_successful_run_date(layout, RunKind.DAILY)
    rid = state_runs.run_id(RunKind.DAILY, target)
    if last is not None and last < target and not layout.run_file(rid).exists():
        days = missed_days(last, target + _dt.timedelta(days=1), max_days=sched.max_catch_up_days)
        if days:
            out.append(CatchUpPlan(RunKind.DAILY, target, len(days), days))

    wtarget = last_weekly_slot(now, sched.weekly_on, sched.weekly_at)
    wlast = state_runs.last_successful_run_date(layout, RunKind.WEEKLY)
    wrid = state_runs.run_id(RunKind.WEEKLY, wtarget)
    if wlast is not None and wlast < wtarget and not layout.run_file(wrid).exists():
        out.append(CatchUpPlan(RunKind.WEEKLY, wtarget, WEEKLY_WINDOW_DAYS, [wtarget]))
    return out
