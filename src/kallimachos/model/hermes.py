"""No model call: Hermes cron feeds the run JSON to the agent, which calls ``kallimachos select``.
``complete`` raises ``UsageError`` (Hermes is a cron + skill role, not a command-line
provider)."""

from __future__ import annotations

from typing import Any

from kallimachos.config.schema import ModelConfig
from kallimachos.interfaces import CheckResult, UsageError


class HermesProvider:
    """Implements :class:`kallimachos.interfaces.Provider`."""

    name = "hermes"

    def __init__(self, cfg: ModelConfig) -> None:
        self.cfg = cfg

    def complete(self, prompt: str, *, timeout_s: int, json_schema: dict[str, Any] | None) -> str:
        raise UsageError(
            "model.provider 'hermes' makes no model call: the Hermes cron agent reads the run JSON "
            "and records its choice with `kallimachos select`"
        )

    def test(self) -> CheckResult:
        return CheckResult(
            ok=True,
            message="hermes: no command-line model; the Hermes cron agent selects with "
            "`kallimachos select` (kallimachos-run skill)",
        )
