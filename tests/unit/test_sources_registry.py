from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from kallimachos.config.schema import Config, PubmedConfig
from kallimachos.interfaces import Source, UsageError
from kallimachos.sources import registry
from kallimachos.sources.base import BaseSource


class PluginSource(BaseSource):
    name = "example_plugin"
    hosts = ("plugin.example.org",)


class _EP:
    def __init__(self, name: str, target: Any, fail: bool = False) -> None:
        self.name = name
        self._target = target
        self._fail = fail

    def load(self) -> Any:
        if self._fail:
            raise ImportError("broken plug-in")
        return self._target


def _fake_entry_points(eps: list[_EP]) -> Any:
    def entry_points(*, group: str) -> Iterator[_EP]:
        assert group == "kallimachos.sources"
        return iter(eps)

    return entry_points


def test_builtins_are_all_registered_and_satisfy_protocol() -> None:
    classes = registry.available_sources()
    assert list(classes)[:10] == list(registry.BUILTIN_NAMES)
    for name in registry.BUILTIN_NAMES:
        src = registry.get_source(name, Config())
        assert isinstance(src, Source)
        assert src.name == name


def test_entry_point_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    eps = [
        _EP("example_plugin", PluginSource),
        _EP("arxiv", PluginSource),  # cannot shadow a built-in
        _EP("broken", None, fail=True),
    ]
    monkeypatch.setattr(registry, "entry_points", _fake_entry_points(eps))
    classes = registry.available_sources()
    assert classes["example_plugin"] is PluginSource
    assert classes["arxiv"].__name__ == "ArxivSource"
    assert "broken" not in classes
    plugin = registry.get_source("example_plugin", Config())
    assert plugin.name == "example_plugin"
    assert plugin not in registry.enabled_sources(Config())


def test_unknown_source_is_usage_error() -> None:
    with pytest.raises(UsageError):
        registry.get_source("google_scholar", Config())


def test_enabled_sources_follow_config() -> None:
    cfg = Config()
    assert [s.name for s in registry.enabled_sources(cfg)] == ["arxiv", "crossref", "openalex"]
    cfg.sources.arxiv.enabled = False
    cfg.sources.zbmath.enabled = True
    assert [s.name for s in registry.enabled_sources(cfg)] == ["crossref", "openalex", "zbmath"]


def test_host_intervals_use_config_and_floors() -> None:
    cfg = Config()
    cfg.sources.arxiv.min_interval_s = 5.0
    cfg.sources.crossref.min_interval_s = 0.1  # below the floor: the floor wins
    hosts = registry.host_intervals(cfg)
    assert hosts["export.arxiv.org"] == 5.0
    assert hosts["api.crossref.org"] == 0.6
    assert hosts["eutils.ncbi.nlm.nih.gov"] == 0.35
    assert hosts["oai.zbmath.org"] == 2.5
    cfg.sources.pubmed = PubmedConfig(api_key_ref="env:EXAMPLE_PUBMED_KEY")
    assert registry.host_intervals(cfg)["eutils.ncbi.nlm.nih.gov"] == 0.1
