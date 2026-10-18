"""``codex exec - --skip-git-repo-check --ephemeral -s read-only --output-schema <file> -o <out> [-m
model]``, prompt on stdin, result read from ``-o``.

Codex takes the schema as a *file* and prints an event stream; the final message is read from the
``-o`` file. Both live in a temporary directory removed after the call.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any

from kallimachos.config.schema import ModelConfig
from kallimachos.interfaces import CheckResult
from kallimachos.model.provider import (
    PROBE_PROMPT,
    PROBE_TIMEOUT_S,
    ModelError,
    base_command,
    run_command,
)


class CodexCliProvider:
    """Implements :class:`kallimachos.interfaces.Provider`."""

    name = "codex"

    def __init__(self, cfg: ModelConfig) -> None:
        self.cfg = cfg

    def command(self, workdir: Path, json_schema: dict[str, Any] | None) -> list[str]:
        cmd = base_command(self.cfg)
        if json_schema is not None:
            schema_file = workdir / "schema.json"
            schema_file.write_text(json.dumps(json_schema), encoding="utf-8")
            cmd += ["--output-schema", str(schema_file)]
        cmd += ["-o", str(workdir / "out.txt")]
        return cmd

    def complete(self, prompt: str, *, timeout_s: int, json_schema: dict[str, Any] | None) -> str:
        with tempfile.TemporaryDirectory(prefix="kallimachos-codex-") as tmp:
            workdir = Path(tmp)
            run_command(self.command(workdir, json_schema), prompt, timeout_s=timeout_s)
            out_file = workdir / "out.txt"
            if not out_file.exists():
                raise ModelError("codex wrote no final message (-o file missing)")
            text = out_file.read_text(encoding="utf-8")
        if not text.strip():
            raise ModelError("codex final message is empty")
        return text

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
            message=f"codex answered: {text.strip()[:40]}",
            latency_s=time.monotonic() - started,
        )
