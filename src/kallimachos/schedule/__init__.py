"""Scheduler back-ends and catch-up."""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kallimachos.config.schema import Config
    from kallimachos.doctor import DoctorCheck
    from kallimachos.paths import DataLayout


def doctor_checks(
    layout: DataLayout,
    config: Config | None,
    *,
    today: _dt.date | None = None,
    **kwargs: Any,
) -> list[DoctorCheck]:
    """Scheduling checks for ``kallimachos doctor`` (imported lazily: ``doctor`` may import this
    package without an import cycle)."""
    from kallimachos.schedule.checks import doctor_checks as _checks

    return _checks(layout, config, today=today, **kwargs)


__all__ = ["doctor_checks"]
