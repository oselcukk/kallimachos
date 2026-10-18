"""``bridge *`` and ``archive confirm`` handlers end to end with a local fake server."""

from __future__ import annotations

import dataclasses
import json

import pytest

from kallimachos import cli_bridge
from kallimachos.cli import main
from kallimachos.config.schema import BridgeConfig, Config
from kallimachos.interfaces import CatalogStatus, ExitCode
from tests.unit import test_bridge_support as _support
from tests.unit.test_bridge_support import (
    World,
    pdf_bytes,
    read_rows,
    write_rows,
)

fake_store = _support.fake_store
world = _support.world


@pytest.fixture
def cli_world(world: World, monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.setattr(cli_bridge, "make_transport", lambda _config: world.transport)
    return world


def _cmd(w: World, *args: str) -> int:
    return main(["--data-dir", str(w.computer.root), *args])


def test_full_round_through_the_cli(cli_world: World, capsys: pytest.CaptureFixture[str]) -> None:
    w = cli_world
    entry = w.outgoing("Algebra", "Author-A_2026_Example.pdf", pdf_bytes("a"))
    assert _cmd(w, "bridge", "pull") == ExitCode.OK
    assert _cmd(w, "bridge", "deliver") == ExitCode.OK
    assert _cmd(w, "bridge", "push-ack") == ExitCode.OK
    capsys.readouterr()
    assert _cmd(w, "--json", "bridge", "status") == ExitCode.OK
    status = json.loads(capsys.readouterr().out)
    assert status["delivered"] == 1 and status["pending_ack"] == 0
    (row,) = read_rows(w.server.catalog_file)
    assert row.id == entry.id and row.status is CatalogStatus.STORED


def test_cli_conflict_and_divergence_exit_3(cli_world: World) -> None:
    w = cli_world
    w.outgoing("Algebra", "Author-A_2026_Example.pdf", pdf_bytes("a"))
    target = w.root / "Algebra" / "Author-A_2026_Example.pdf"
    target.parent.mkdir()
    target.write_bytes(b"%PDF other")
    assert _cmd(w, "bridge", "pull") == ExitCode.OK
    assert _cmd(w, "bridge", "deliver") == ExitCode.PARTIAL
    assert target.read_bytes() == b"%PDF other"

    rows = read_rows(w.computer.catalog_file)
    extra = dataclasses.replace(rows[0], id="cccccccccccc", sha256="c" * 64)
    write_rows(w.computer.catalog_file, [*rows, extra])
    assert _cmd(w, "bridge", "pull") == ExitCode.PARTIAL
    assert _cmd(w, "bridge", "reconcile") == ExitCode.OK


def test_archive_confirm_on_the_server(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    entry = world.outgoing("Algebra", "Author-A_2026_Example.pdf", pdf_bytes("a"))
    assert entry.sha256
    server = ["--data-dir", str(world.server.root)]
    assert main([*server, "--dry-run", "archive", "confirm", "--sha", entry.sha256]) == 0
    assert read_rows(world.server.catalog_file)[0].status is CatalogStatus.OUTGOING
    assert main([*server, "archive", "confirm", "--sha", entry.sha256]) == 0
    assert read_rows(world.server.catalog_file)[0].status is CatalogStatus.STORED
    assert main([*server, "archive", "confirm", "--sha", "0" * 64]) == ExitCode.USAGE
    capsys.readouterr()


def test_disabled_bridge_is_a_usage_error() -> None:
    with pytest.raises(Exception, match=r"bridge\.enabled"):
        cli_bridge.make_transport(Config())
    cfg = dataclasses.replace(Config(), bridge=BridgeConfig(enabled=True))
    with pytest.raises(Exception, match="remote"):
        cli_bridge.make_transport(cfg)
