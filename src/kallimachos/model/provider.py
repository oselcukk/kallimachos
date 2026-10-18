"""Provider factory and the shared subprocess runner.

``model.command`` (null = provider default) is the full base command line, split with
``shlex``; use it to add ``--model <m>`` or ``--max-budget-usd <usd>`` (claude) or ``-m <m>``
(codex). The provider appends its own schema/output flags.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass

from kallimachos.config.schema import ModelConfig
from kallimachos.interfaces import KallimachosError, Provider, UsageError

_DEFAULT_COMMANDS: dict[str, tuple[str, ...]] = {
    "claude": (
        "claude",
        "-p",
        "--output-format",
        "json",
        "--tools",
        "",
        "--no-session-persistence",
    ),
    "codex": ("codex", "exec", "-", "--skip-git-repo-check", "--ephemeral", "-s", "read-only"),
}

PROBE_PROMPT = "Reply with the single word OK."
PROBE_TIMEOUT_S = 120


class ModelError(KallimachosError):
    """The model command failed, timed out or returned unusable output (callers fall back to the
    deterministic path)."""


@dataclass
class ProcessOutput:
    stdout: str
    stderr: str
    returncode: int
    elapsed_s: float


def get_provider(cfg: ModelConfig) -> Provider:
    """``claude`` | ``codex`` | ``hermes`` | ``none`` -> instance."""
    if cfg.provider == "claude":
        from kallimachos.model.claude_cli import ClaudeCliProvider

        return ClaudeCliProvider(cfg)
    if cfg.provider == "codex":
        from kallimachos.model.codex_cli import CodexCliProvider

        return CodexCliProvider(cfg)
    if cfg.provider == "hermes":
        from kallimachos.model.hermes import HermesProvider

        return HermesProvider(cfg)
    if cfg.provider == "none":
        from kallimachos.model.none import NoneProvider

        return NoneProvider()
    raise UsageError(f"unknown model provider {cfg.provider!r}")


def default_command(provider: str) -> list[str]:
    """Provider default command line (``claude -p ...``, ``codex exec ...``)."""
    try:
        return list(_DEFAULT_COMMANDS[provider])
    except KeyError:
        raise UsageError(
            f"provider {provider!r} has no command line (only claude and codex)"
        ) from None


def with_safe_flags(provider: str, parts: list[str]) -> list[str]:
    """A custom ``model.command`` keeps the flags that deny the model any tool or write access:
    titles and abstracts it reads are untrusted text, and it only has to answer with JSON.
    ``claude`` gets ``--tools ""`` unless ``--tools``/``--allowedTools`` is set explicitly;
    ``codex`` gets ``-s read-only`` unless a sandbox is set explicitly."""
    if provider == "claude" and not any(
        p in ("--tools", "--allowedTools", "--allowed-tools", "--disallowedTools")
        or p.startswith(("--tools=", "--allowedTools=", "--allowed-tools="))
        for p in parts
    ):
        return [*parts, "--tools", ""]
    if provider == "codex" and not any(
        p in ("-s", "--sandbox") or p.startswith("--sandbox=") for p in parts
    ):
        return [*parts, "-s", "read-only"]
    return parts


def base_command(cfg: ModelConfig) -> list[str]:
    """``model.command`` split with shlex (plus the safe flags, :func:`with_safe_flags`), else
    :func:`default_command`."""
    if cfg.command:
        parts = shlex.split(cfg.command)
        if not parts:
            raise UsageError("model.command is empty")
        return with_safe_flags(cfg.provider, parts)
    return default_command(cfg.provider)


def in_ci(env: dict[str, str] | None = None) -> bool:
    env = dict(os.environ) if env is None else env
    return env.get("CI", "").lower() == "true" or env.get("GITHUB_ACTIONS", "").lower() == "true"


def run_command(cmd: list[str], prompt: str, *, timeout_s: int) -> ProcessOutput:
    """Run ``cmd`` with the prompt on stdin. Missing binary, timeout and non-zero exit raise
    :class:`ModelError`; the prompt is never echoed into the error."""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError:
        raise ModelError(f"model command not found: {cmd[0]!r}") from None
    except subprocess.TimeoutExpired:
        raise ModelError(f"model command timed out after {timeout_s} s") from None
    out = ProcessOutput(
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        returncode=proc.returncode,
        elapsed_s=time.monotonic() - started,
    )
    if out.returncode != 0:
        tail = out.stderr.strip().splitlines()[-1:] or out.stdout.strip().splitlines()[-1:]
        raise ModelError(f"model command exited {out.returncode}: {(tail or [''])[0][:200]}")
    return out
