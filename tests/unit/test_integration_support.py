"""Shared helpers of the ``test_integration_*`` modules: all real modules, no network
(recorded fixtures through :class:`tests.unit.fakes.FixtureHttp`), one isolated data directory.

The only seams are ``http.client.from_config`` (replaced by a fixture-backed client) and the
external tools (``pdftotext``, ``pdfinfo``) that may be missing on the test machine.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from kallimachos.cli import main
from kallimachos.http import client as http_client
from kallimachos.interfaces import ExitCode
from kallimachos.paths import DataLayout
from tests.unit.fakes import FixtureTransport
from tests.unit.test_sources_fakes import fixture

REPO = Path(__file__).resolve().parents[2]
ANSWERS = REPO / "tests" / "e2e" / "answers_qa.yaml"


def run_cli(*argv: str, expect: int | ExitCode | None = ExitCode.OK) -> int:
    code = main(list(argv))
    if expect is not None:
        assert code == int(expect), f"{argv} exited {code}"
    return code


def edit_config(layout: DataLayout, **changes: Any) -> None:
    """Set dotted keys in ``config.yaml`` (``archive__enabled=True`` -> ``archive.enabled``)."""
    data = yaml.safe_load(layout.config_file.read_text(encoding="utf-8")) or {}
    for dotted, value in changes.items():
        node = data
        parts = dotted.split("__")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    layout.config_file.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def recorded_http(monkeypatch: pytest.MonkeyPatch, *routes: tuple[str, Any]) -> FixtureTransport:
    """Keep the real ``http.client.from_config`` (real ``Http`` with cache, budget, pacing and
    secret rules) but give it a transport that replays recorded responses by URL prefix and never
    sleeps."""
    transport = FixtureTransport(*routes)
    real = http_client.from_config

    def factory(config: Any, layout: Any, **kw: Any) -> http_client.Http:
        kw["transport"] = transport
        kw["offline"] = False
        http = real(config, layout, **kw)
        http._sleep = lambda _s: None  # per-host pacing is tested in test_core_http
        return http

    monkeypatch.setattr(http_client, "from_config", factory)
    return transport


def source_fixtures() -> list[tuple[str, Any]]:
    """Recorded answers of the three default sources of the Q&A profile."""
    return [
        ("https://export.arxiv.org/", fixture("arxiv", "list")),
        ("http://export.arxiv.org/", fixture("arxiv", "list")),
        ("https://api.crossref.org/", fixture("crossref", "journal_works")),
        ("https://api.openalex.org/", fixture("openalex", "works_list")),
    ]
