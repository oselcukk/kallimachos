"""End-to-end tests. Offline by default.

Tests marked ``e2e_network`` need the internet (they download the official avenoxbeyin release)
and run only with ``KALLIMACHOS_E2E_NETWORK=1``::

    KALLIMACHOS_E2E_NETWORK=1 pytest -m e2e_network tests/e2e

For those tests the suite-wide socket block is lifted; every other test keeps it.
"""

from __future__ import annotations

import os
import socket

import pytest

from tests.conftest import _blocked

NETWORK_ENV = "KALLIMACHOS_E2E_NETWORK"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "e2e_network: needs the internet; runs only with KALLIMACHOS_E2E_NETWORK=1"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get(NETWORK_ENV) == "1":
        return
    skip = pytest.mark.skip(reason=f"network e2e test; set {NETWORK_ENV}=1 to run it")
    for item in items:
        if "e2e_network" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same block as the suite-wide fixture, except for ``e2e_network`` tests."""
    if request.node.get_closest_marker("e2e_network") is not None:
        return
    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    monkeypatch.setenv("OFFLINE", "1")
