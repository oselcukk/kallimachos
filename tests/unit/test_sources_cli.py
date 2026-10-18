from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kallimachos import cli, cli_sources
from kallimachos.config.schema import Config
from kallimachos.paths import DataLayout
from tests.unit.test_sources_fakes import Fail, FakeHttp, fixture

SECRET = "SENTINEL-KEY-7f3a9c"


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    cfg = Config()
    cfg.sources.openalex.api_key_ref = "env:EXAMPLE_OPENALEX_KEY"
    cfg.sources.pubmed.api_key_ref = "keyring:kallimachos/pubmed"
    state: dict[str, Any] = {"config": cfg, "http": FakeHttp()}
    monkeypatch.setattr(cli_sources, "_load", lambda args: (state["config"], DataLayout(tmp_path)))
    monkeypatch.setattr(cli_sources, "build_http", lambda config, layout: state["http"])
    present = {"env:EXAMPLE_OPENALEX_KEY": True}
    monkeypatch.setattr("kallimachos.secrets.is_present", lambda ref: present.get(ref, False))
    monkeypatch.setattr("kallimachos.secrets.resolve_ref", lambda ref: SECRET)
    return state


def test_sources_list_shows_presence_only(
    setup: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["sources", "list"]) == 0
    out = capsys.readouterr().out
    assert "arxiv" in out and "scholar_imap" in out
    assert "api_key: present" in out and "api_key: absent" in out
    assert SECRET not in out and "EXAMPLE_OPENALEX_KEY" not in out


def test_sources_list_json(setup: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--json", "sources", "list"]) == 0
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}
    assert len(rows) >= 10
    assert rows["openalex"]["keys"] == {"api_key": "present"}
    assert rows["pubmed"]["keys"] == {"api_key": "absent", "email": "not set"}
    assert rows["arxiv"]["enabled"] is True and rows["arxiv"]["min_interval_s"] == 3.1
    assert rows["zbmath"]["hosts"] == ["oai.zbmath.org", "api.zbmath.org"]


def test_sources_test_ok_and_failure(
    setup: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    setup["http"] = FakeHttp().add("https://export.arxiv.org/", fixture("arxiv", "empty"))
    assert cli.main(["sources", "test", "arxiv"]) == 0
    assert "arxiv: ok HTTP 200" in capsys.readouterr().out
    setup["http"] = (
        FakeHttp()
        .add("https://export.arxiv.org/", Fail(503))
        .add("http://export.arxiv.org/", Fail(503))
    )
    assert cli.main(["sources", "test", "arxiv"]) == 6
    assert "FAILED" in capsys.readouterr().err


def test_sources_test_json_never_prints_key(
    setup: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    setup["http"] = FakeHttp().add("https://api.openalex.org/", fixture("openalex", "works_list"))
    assert cli.main(["--json", "sources", "test", "openalex"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["ok"] is True and data["key_present"] is True
    assert SECRET not in out
    assert setup["http"].calls[0].headers == {"Authorization": f"Bearer {SECRET}"}


def test_unknown_source_exits_2(setup: dict[str, Any]) -> None:
    assert cli.main(["sources", "test", "google_scholar"]) == 2


def test_build_http_uses_host_intervals(tmp_path: Path) -> None:
    from kallimachos.http.client import Http

    http = cli_sources.build_http(Config(), DataLayout(tmp_path))
    assert isinstance(http, Http)
    assert http.policy.min_interval_s["export.arxiv.org"] == 3.1
    assert http.offline is True  # OFFLINE=1 in the test suite


def test_sources_test_without_a_name_tests_every_enabled_source(
    setup: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    cfg: Config = setup["config"]
    for name in ("arxiv", "crossref", "openalex", "pubmed", "iacr_eprint", "zbmath"):
        getattr(cfg.sources, name).enabled = name in ("arxiv", "openalex")
    setup["http"] = (
        FakeHttp()
        .add("https://export.arxiv.org/", fixture("arxiv", "empty"))
        .add("https://api.openalex.org/", fixture("openalex", "works_list"))
    )
    assert cli.main(["sources", "test"]) == 0
    out = capsys.readouterr().out
    assert "arxiv: ok" in out and "openalex: ok" in out and "crossref" not in out
    setup["http"] = (
        FakeHttp()
        .add("https://export.arxiv.org/", Fail(503))
        .add("http://export.arxiv.org/", Fail(503))
        .add("https://api.openalex.org/", fixture("openalex", "works_list"))
    )
    assert cli.main(["--json", "sources", "test"]) == 6
    report = json.loads(capsys.readouterr().out)
    assert set(report) == {"arxiv", "openalex"}
    assert report["arxiv"]["ok"] is False and report["openalex"]["ok"] is True
