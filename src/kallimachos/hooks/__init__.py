"""Optional agent hooks for Claude Code and Codex.

* ``SessionStart``: a short status summary (``hooks.summary``).
* ``PreToolUse``: a guard that keeps state files, the catalog and managed note blocks out of reach
  of file tools and names the right ``kallimachos`` command (``hooks.guard``).

There is deliberately no receipt hook: an Avenox vault records receipts itself (no double record).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kallimachos.config.schema import Config
    from kallimachos.doctor import DoctorCheck
    from kallimachos.paths import DataLayout


def doctor_checks(layout: DataLayout, config: Config | None, **kwargs: Any) -> list[DoctorCheck]:
    """Hook checks for ``kallimachos doctor`` (lazy import, no cycle with ``doctor``)."""
    from kallimachos.hooks.checks import doctor_checks as _checks

    return _checks(layout, config, **kwargs)


__all__ = ["doctor_checks"]
