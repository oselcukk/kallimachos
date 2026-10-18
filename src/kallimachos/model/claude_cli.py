"""``claude -p --output-format json --json-schema <schema> --tools (empty string)
--no-session-persistence [--model m] [--max-budget-usd u]``, prompt on stdin; ``--bare`` in CI.

``--output-format json`` prints one result object. With ``--json-schema`` the validated object is
read from ``structured_output`` when present, otherwise ``result`` (the final text) is returned;
``is_error: true`` is a :class:`ModelError`.
"""

from __future__ import annotations

import json
import time
from typing import Any

from kallimachos.config.schema import ModelConfig
from kallimachos.interfaces import CheckResult
from kallimachos.model.provider import (
    PROBE_PROMPT,
    PROBE_TIMEOUT_S,
    ModelError,
    base_command,
    in_ci,
    run_command,
)


class ClaudeCliProvider:
    """Implements :class:`kallimachos.interfaces.Provider`."""

    name = "claude"

    def __init__(self, cfg: ModelConfig) -> None:
        self.cfg = cfg

    def command(self, json_schema: dict[str, Any] | None) -> list[str]:
        cmd = base_command(self.cfg)
        if in_ci() and "--bare" not in cmd:
            cmd.append("--bare")
        if json_schema is not None:
            cmd += ["--json-schema", json.dumps(json_schema, separators=(",", ":"))]
        return cmd

    def complete(self, prompt: str, *, timeout_s: int, json_schema: dict[str, Any] | None) -> str:
        out = run_command(self.command(json_schema), prompt, timeout_s=timeout_s)
        return parse_output(out.stdout)

    def test(self) -> CheckResult:
        started = time.monotonic()
        try:
            text = self.complete(
                PROBE_PROMPT, timeout_s=min(PROBE_TIMEOUT_S, self.cfg.timeout_s), json_schema=None
            )
        except ModelError as exc:
            return CheckResult(ok=False, message=str(exc))
        return CheckResult(
            ok=True,
            message=f"claude answered: {text.strip()[:40]}",
            latency_s=time.monotonic() - started,
        )


def parse_output(stdout: str) -> str:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        raise ModelError("claude output is not JSON (expected --output-format json)") from None
    if not isinstance(data, dict):
        raise ModelError("claude output is not a JSON object")
    if data.get("is_error"):
        raise ModelError(f"claude reported an error: {str(data.get('result', ''))[:200]}")
    structured = data.get("structured_output")
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)
    result = data.get("result")
    if not isinstance(result, str):
        raise ModelError("claude output has no result text")
    return result
