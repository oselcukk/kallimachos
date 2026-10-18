"""Bridge transports: LocalTransport on a temp dir, SshRsyncTransport argv
with a fake runner (no real ssh, no network)."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import pytest

from kallimachos.bridge.base import LocalTransport, TransportError, kallimachos_argv
from kallimachos.bridge.ssh_rsync import SshRsyncTransport


def test_local_pull_copies_only_regular_matching_files(tmp_path: Path) -> None:
    remote = tmp_path / "remote"
    (remote / "archive/outgoing/A").mkdir(parents=True)
    (remote / "archive/outgoing/A/x.pdf").write_bytes(b"x")
    (remote / "archive/outgoing/A/notes.txt").write_text("n")
    os.symlink(remote / "archive/outgoing/A/x.pdf", remote / "archive/outgoing/A/link.pdf")
    local = tmp_path / "cache"
    t = LocalTransport(remote)
    t.pull("archive/outgoing/", local, pattern="*.pdf")
    assert sorted(p.relative_to(local).as_posix() for p in local.rglob("*")) == ["A", "A/x.pdf"]

    t.pull("archive/missing/", local / "m", missing_ok=True)
    t.pull("archive/missing.jsonl", local / "m.jsonl", missing_ok=True)
    assert not (local / "m").exists() and not (local / "m.jsonl").exists()
    with pytest.raises(TransportError):
        t.pull("archive/missing.jsonl", local / "m.jsonl")
    with pytest.raises(TransportError):
        t.pull("../escape", local / "e")


def test_local_push_never_replaces(tmp_path: Path) -> None:
    remote = tmp_path / "remote"
    src = tmp_path / "rows.jsonl"
    src.write_text("{}\n")
    t = LocalTransport(remote)
    t.push(src, "bridge/inbox/a.jsonl")
    assert (remote / "bridge/inbox/a.jsonl").read_text() == "{}\n"
    src.write_text("changed\n")
    with pytest.raises(TransportError):
        t.push(src, "bridge/inbox/a.jsonl")
    assert (remote / "bridge/inbox/a.jsonl").read_text() == "{}\n"


def test_local_run_uses_custom_runner(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    t = LocalTransport(tmp_path, runner=lambda argv: (calls.append(argv), (0, "ok", ""))[1])
    assert t.run(kallimachos_argv(t, "bridge", "reconcile")) == (0, "ok", "")
    assert calls == [["kallimachos", "--data-dir", str(tmp_path), "bridge", "reconcile"]]


class FakeRunner:
    def __init__(self, codes: list[int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.codes = list(codes or [])

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        code = self.codes.pop(0) if self.codes else 0
        return code, "", "boom" if code else ""


def _ssh(runner: FakeRunner, tmp_path: Path, retry: int = 1) -> SshRsyncTransport:
    return SshRsyncTransport(
        "user@server.invalid",
        "/srv/kallimachos data",
        ssh_options=["BatchMode=yes", "ConnectTimeout=20"],
        retry=retry,
        runner=runner,
        sleep=lambda _s: None,
        control_dir=tmp_path,
    )


def test_ssh_single_master_and_reuse(tmp_path: Path) -> None:
    runner = FakeRunner()
    t = _ssh(runner, tmp_path)
    t.pull("archive/outgoing/", tmp_path / "cache", pattern="*.pdf")
    t.pull("archive/catalog.jsonl", tmp_path / "cache.jsonl")
    code, _out, _err = t.run(["kallimachos", "archive", "confirm", "--sha", "ab"])
    t.close()
    assert code == 0
    master, pull_dir, pull_file, run, close = runner.calls
    assert master[0] == "ssh" and "-fN" in master
    assert "ControlMaster=yes" in master and "ControlPersist=no" in master
    assert "BatchMode=yes" in master and f"ControlPath={tmp_path / 'cm'}" in master
    assert pull_dir[:2] == ["rsync", "-rt"] and "-l" not in pull_dir and "-a" not in pull_dir
    assert "--include=*.pdf" in pull_dir and pull_dir[-1].endswith("/")
    assert pull_dir[-2] == "user@server.invalid:" + shlex.quote(
        "/srv/kallimachos data/archive/outgoing/"
    )
    assert "ControlMaster=no" in pull_dir[pull_dir.index("-e") + 1]
    assert pull_file[-2].endswith("archive/catalog.jsonl'")
    assert run[-2:] == ["--", "kallimachos archive confirm --sha ab"]
    assert close[-3:] == ["-O", "exit", "user@server.invalid"]


def test_ssh_retry_is_bounded(tmp_path: Path) -> None:
    runner = FakeRunner(codes=[255, 255, 0])
    with pytest.raises(TransportError):
        _ssh(runner, tmp_path, retry=1).open()
    assert len(runner.calls) == 2  # first try + one retry, fail2ban friendly

    runner = FakeRunner(codes=[255, 0])
    _ssh(runner, tmp_path, retry=1).open()
    assert len(runner.calls) == 2


def test_ssh_push_creates_dir_and_never_replaces(tmp_path: Path) -> None:
    runner = FakeRunner()
    t = _ssh(runner, tmp_path)
    t.push(tmp_path / "outbox.jsonl", "bridge/inbox/x.jsonl")
    _master, mkdir, rsync = runner.calls
    assert mkdir[-1] == shlex.join(["mkdir", "-p", "/srv/kallimachos data/bridge/inbox"])
    assert "--ignore-existing" in rsync


def test_ssh_rejects_bad_remote_and_traversal(tmp_path: Path) -> None:
    with pytest.raises(TransportError):
        SshRsyncTransport("-oProxyCommand=x", "/d", ssh_options=[], retry=0)
    t = _ssh(FakeRunner(), tmp_path)
    with pytest.raises(TransportError):
        t.pull("../etc/passwd", tmp_path / "x")


def test_ssh_adds_batchmode_when_missing(tmp_path: Path) -> None:
    runner = FakeRunner()
    t = SshRsyncTransport(
        "user@server.invalid", "/d", ssh_options=[], retry=0, runner=runner, control_dir=tmp_path
    )
    t.open()
    assert "BatchMode=yes" in runner.calls[0]
