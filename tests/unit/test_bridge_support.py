"""Shared helpers for the bridge tests: a fake server data dir, a computer data dir, an
archive root, and minimal stand-ins for ``state.store`` / ``config.load``.

The stand-ins follow the documented contracts (atomic tmp + replace, NFC, JSON lines) so the bridge
code under test is exercised exactly as it runs with the real store.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import tempfile
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from kallimachos.bridge.base import LocalTransport
from kallimachos.config.schema import ArchiveConfig, BridgeConfig, Config
from kallimachos.interfaces import (
    CatalogEntry,
    CatalogEvent,
    CatalogEventType,
    CatalogStatus,
    UnreachableEntry,
    dump,
)
from kallimachos.paths import DataLayout

T0 = dt.datetime(2026, 1, 5, 6, 30, tzinfo=dt.UTC)


# --------------------------------------------------------------------------- store stand-in


def _nfc(data: Any) -> Any:
    if isinstance(data, str):
        return unicodedata.normalize("NFC", data)
    if isinstance(data, Mapping):
        return {_nfc(k): _nfc(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_nfc(v) for v in data]
    return data


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def fake_read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def fake_append_jsonl(
    path: Path, record: Mapping[str, Any], *, schema: Mapping[str, Any] | None = None
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_nfc(dict(record)), ensure_ascii=False) + "\n")


def fake_write_text_atomic(path: Path, text: str) -> None:
    _atomic(path, unicodedata.normalize("NFC", text))


def fake_write_json_atomic(
    path: Path, data: Any, *, schema: Mapping[str, Any] | None = None
) -> None:
    _atomic(path, json.dumps(_nfc(data), ensure_ascii=False, indent=2))


def fake_read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- world


@dataclass
class World:
    tmp: Path
    server: DataLayout
    computer: DataLayout
    root: Path
    config: Config
    server_config: Config

    @property
    def transport(self) -> LocalTransport:
        return LocalTransport(self.server.root)

    def outgoing(self, folder: str, name: str, data: bytes, **extra: Any) -> CatalogEntry:
        """Server side: a downloaded PDF waiting in ``archive/outgoing`` + catalog row."""
        path = self.server.archive / "outgoing" / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        digest = sha(data)
        entry = CatalogEntry(
            id=digest[:12],
            keys=extra.pop("keys", [f"doi:10.0000/example.{digest[:6]}"]),
            title=extra.pop("title", "An Example Title"),
            authors=["Example Author A"],
            year=2026,
            folder=folder,
            filename=name,
            path=f"{folder}/{name}",
            sha256=digest,
            size=len(data),
            status=CatalogStatus.OUTGOING,
            events=[CatalogEvent(at=T0, type=CatalogEventType.OUTGOING, detail={})],
            **extra,
        )
        self.add_server_rows([entry])
        return entry

    def add_server_rows(self, rows: list[CatalogEntry]) -> None:
        write_rows(self.server.catalog_file, [*read_rows(self.server.catalog_file), *rows])

    def add_unreachable(self, entry: UnreachableEntry) -> None:
        fake_append_jsonl(self.server.unreachable_file, dump(entry))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pdf_bytes(label: str) -> bytes:
    return b"%PDF-1.4\n% synthetic test file\n" + label.encode() + b"\n%%EOF\n"


def read_rows(path: Path) -> list[CatalogEntry]:
    from kallimachos.bridge.catalog_copy import load_entries

    return load_entries(path)


def write_rows(path: Path, rows: list[CatalogEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(dump(r)) + "\n" for r in rows), encoding="utf-8")


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kept for the fixture name only: the bridge tests run on the real ``state.store``."""


@pytest.fixture
def world(tmp_path: Path, fake_store: None, monkeypatch: pytest.MonkeyPatch) -> World:
    server = DataLayout(tmp_path / "server-home")
    computer = DataLayout(tmp_path / "computer-home")
    root = tmp_path / "Archive"
    for d in (server.archive, computer.archive, root):
        d.mkdir(parents=True)
    config = dataclasses.replace(
        Config(),
        archive=ArchiveConfig(enabled=True, root=str(root)),
        bridge=BridgeConfig(
            enabled=True,
            remote="user@server.invalid",
            remote_data_dir=str(server.root),
            local_archive_root=str(root),
        ),
    )
    server_config = dataclasses.replace(Config(), archive=ArchiveConfig(enabled=True))
    configs = {str(server.root): server_config, str(computer.root): config}

    def fake_load_config(
        path: Path | None = None, *, data_dir: str | None = None
    ) -> tuple[Config, DataLayout]:
        key = str(data_dir or computer.root)
        return configs.get(key, Config()), DataLayout(Path(key))

    from kallimachos.config import load

    monkeypatch.setattr(load, "load_config", fake_load_config)
    return World(tmp_path, server, computer, root, config, server_config)


def snapshot(root: Path) -> dict[str, bytes]:
    """Every regular file under ``root`` with its bytes (to prove nothing was overwritten)."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }
