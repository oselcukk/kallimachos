"""Model providers: documented command lines, output parsing; subprocess is faked."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from kallimachos.config.schema import ModelConfig
from kallimachos.interfaces import Provider, UsageError
from kallimachos.model import provider as provider_mod
from kallimachos.model.claude_cli import ClaudeCliProvider
from kallimachos.model.codex_cli import CodexCliProvider
from kallimachos.model.hermes import HermesProvider
from kallimachos.model.none import NoneProvider
from kallimachos.model.provider import ModelError, default_command, get_provider

SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}}


class FakeRun:
    """Stands in for subprocess.run; records calls, writes codex -o files."""

    def __init__(self, stdout: str = "", returncode: int = 0, out_text: str | None = None) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.out_text = out_text
        self.calls: list[dict[str, Any]] = []

    def __call__(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"cmd": list(cmd), **kw})
        if self.out_text is not None and "-o" in cmd:
            Path(cmd[cmd.index("-o") + 1]).write_text(self.out_text, encoding="utf-8")
        if "--output-schema" in cmd:
            schema_file = Path(cmd[cmd.index("--output-schema") + 1])
            self.calls[-1]["schema"] = json.loads(schema_file.read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "boom\n")


@pytest.fixture(autouse=True)
def _no_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


def test_factory_returns_the_four_providers() -> None:
    for name, cls in (
        ("claude", ClaudeCliProvider),
        ("codex", CodexCliProvider),
        ("hermes", HermesProvider),
        ("none", NoneProvider),
    ):
        prov = get_provider(ModelConfig(provider=name))  # type: ignore[arg-type]
        assert isinstance(prov, cls)
        assert isinstance(prov, Provider)
        assert prov.name == name


def test_default_commands_match_spec() -> None:
    assert default_command("claude") == [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--tools",
        "",
        "--no-session-persistence",
    ]
    assert default_command("codex") == [
        "codex",
        "exec",
        "-",
        "--skip-git-repo-check",
        "--ephemeral",
        "-s",
        "read-only",
    ]
    with pytest.raises(UsageError):
        default_command("hermes")


def test_claude_builds_command_and_reads_structured_output(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRun(
        stdout=json.dumps({"type": "result", "result": "", "structured_output": {"x": "y"}})
    )
    monkeypatch.setattr(subprocess, "run", fake)
    out = ClaudeCliProvider(ModelConfig(provider="claude")).complete(
        "PROMPT", timeout_s=30, json_schema=SCHEMA
    )
    assert json.loads(out) == {"x": "y"}
    call = fake.calls[0]
    cmd = call["cmd"]
    assert cmd[:7] == default_command("claude")
    assert cmd[cmd.index("--json-schema") + 1] == json.dumps(SCHEMA, separators=(",", ":"))
    assert "--bare" not in cmd
    assert call["input"] == "PROMPT"  # prompt on stdin, never on the command line
    assert "PROMPT" not in cmd
    assert call["timeout"] == 30


def test_claude_result_text_and_bare_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    fake = FakeRun(stdout=json.dumps({"type": "result", "is_error": False, "result": "OK"}))
    monkeypatch.setattr(subprocess, "run", fake)
    prov = ClaudeCliProvider(ModelConfig(provider="claude", command="claude -p --model opus"))
    assert prov.complete("p", timeout_s=5, json_schema=None) == "OK"
    cmd = fake.calls[0]["cmd"]
    assert cmd[:4] == ["claude", "-p", "--model", "opus"]
    assert "--bare" in cmd and "--json-schema" not in cmd


@pytest.mark.parametrize(
    "stdout",
    ["not json", json.dumps({"is_error": True, "result": "quota"}), json.dumps({"result": 3})],
)
def test_claude_bad_output_is_model_error(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    monkeypatch.setattr(subprocess, "run", FakeRun(stdout=stdout))
    with pytest.raises(ModelError):
        ClaudeCliProvider(ModelConfig(provider="claude")).complete(
            "p", timeout_s=5, json_schema=None
        )


def test_nonzero_exit_timeout_and_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    prov = ClaudeCliProvider(ModelConfig(provider="claude"))
    monkeypatch.setattr(subprocess, "run", FakeRun(returncode=2))
    with pytest.raises(ModelError, match="exited 2"):
        prov.complete("p", timeout_s=5, json_schema=None)

    def timeout(*_a: Any, **_k: Any) -> Any:
        raise subprocess.TimeoutExpired("claude", 5)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ModelError, match="timed out"):
        prov.complete("p", timeout_s=5, json_schema=None)

    def missing(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("claude")

    monkeypatch.setattr(subprocess, "run", missing)
    result = prov.test()
    assert not result.ok and "not found" in result.message


def test_codex_schema_file_and_output_file(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRun(stdout='{"type":"event"}\n', out_text='{"x": "z"}')
    monkeypatch.setattr(subprocess, "run", fake)
    out = CodexCliProvider(
        ModelConfig(provider="codex", command="codex exec - -m example")
    ).complete("PROMPT", timeout_s=60, json_schema=SCHEMA)
    assert json.loads(out) == {"x": "z"}
    call = fake.calls[0]
    cmd = call["cmd"]
    assert cmd[:5] == ["codex", "exec", "-", "-m", "example"]
    assert call["schema"] == SCHEMA  # schema handed over as a file
    assert "-o" in cmd and call["input"] == "PROMPT"
    assert not Path(cmd[cmd.index("-o") + 1]).exists()  # temp dir cleaned up


def test_codex_default_command_and_missing_output(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRun(stdout="")
    monkeypatch.setattr(subprocess, "run", fake)
    with pytest.raises(ModelError, match="-o"):
        CodexCliProvider(ModelConfig(provider="codex")).complete("p", timeout_s=5, json_schema=None)
    assert fake.calls[0]["cmd"][:7] == default_command("codex")
    assert "--output-schema" not in fake.calls[0]["cmd"]


def test_codex_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", FakeRun(out_text="OK"))
    result = CodexCliProvider(ModelConfig(provider="codex")).test()
    assert result.ok and result.latency_s is not None


def test_hermes_refuses_complete_but_test_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    prov = HermesProvider(ModelConfig(provider="hermes"))
    with pytest.raises(UsageError, match="select"):
        prov.complete("p", timeout_s=5, json_schema=None)
    assert prov.test().ok
    assert calls == []


def test_none_refuses_complete() -> None:
    with pytest.raises(UsageError):
        NoneProvider().complete("p", timeout_s=5, json_schema=None)
    assert NoneProvider().test().ok


def test_empty_command_is_usage_error() -> None:
    with pytest.raises(UsageError):
        provider_mod.base_command(ModelConfig(provider="claude", command="   "))


def test_custom_command_keeps_the_safe_flags() -> None:
    from kallimachos.model.provider import base_command

    claude = base_command(ModelConfig(provider="claude", command="claude -p --model x"))
    assert claude[-2:] == ["--tools", ""]
    explicit = base_command(ModelConfig(provider="claude", command='claude -p --tools "Read"'))
    assert explicit.count("--tools") == 1 and explicit[-1] == "Read"
    codex = base_command(ModelConfig(provider="codex", command="codex exec - -m gpt"))
    assert codex[-2:] == ["-s", "read-only"]
    sandboxed = base_command(ModelConfig(provider="codex", command="codex exec --sandbox danger"))
    assert sandboxed.count("--sandbox") == 1 and "-s" not in sandboxed
