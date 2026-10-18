"""Test-wide guards: no network, isolated data directory, OFFLINE mode."""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


class NetworkBlocked(RuntimeError):
    pass


def _blocked(*_args: object, **_kwargs: object) -> None:
    raise NetworkBlocked("network access is disabled in the test suite (tests are offline)")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every attempt to open a socket fails loudly. Tests that need HTTP use fixture fakes."""
    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    monkeypatch.setenv("OFFLINE", "1")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Each test gets its own KALLIMACHOS_HOME so nothing touches the developer's data."""
    home = tmp_path / "kallimachos-home"
    home.mkdir()
    monkeypatch.setenv("KALLIMACHOS_HOME", str(home))
    yield home


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def fixtures_dir() -> Path:
    return REPO_ROOT / "tests" / "fixtures"


def pytest_configure(config: pytest.Config) -> None:
    os.environ.setdefault("OFFLINE", "1")
