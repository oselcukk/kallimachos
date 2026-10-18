"""Source registry. Built-ins plus ``kallimachos.sources`` entry points."""

from __future__ import annotations

import importlib
import logging
from importlib.metadata import entry_points
from typing import Any, cast

from kallimachos.config.schema import DEFAULT_MIN_INTERVAL_S, Config
from kallimachos.interfaces import Source, UsageError

ENTRY_POINT_GROUP = "kallimachos.sources"

BUILTIN_NAMES: tuple[str, ...] = (
    "arxiv",
    "iacr_eprint",
    "crossref",
    "openalex",
    "semantic_scholar",
    "zbmath",
    "pubmed",
    "biorxiv",
    "rss",
    "scholar_imap",
)

_BUILTIN_CLASSES: dict[str, str] = {
    "arxiv": "kallimachos.sources.arxiv:ArxivSource",
    "iacr_eprint": "kallimachos.sources.iacr_eprint:IacrEprintSource",
    "crossref": "kallimachos.sources.crossref:CrossrefSource",
    "openalex": "kallimachos.sources.openalex:OpenAlexSource",
    "semantic_scholar": "kallimachos.sources.semantic_scholar:SemanticScholarSource",
    "zbmath": "kallimachos.sources.zbmath:ZbmathSource",
    "pubmed": "kallimachos.sources.pubmed:PubmedSource",
    "biorxiv": "kallimachos.sources.biorxiv:BiorxivSource",
    "rss": "kallimachos.sources.rss:RssSource",
    "scholar_imap": "kallimachos.sources.scholar_imap:ScholarImapSource",
}

log = logging.getLogger(__name__)


def _load(target: str) -> type[Source]:
    module, _, attr = target.partition(":")
    return cast(type[Source], getattr(importlib.import_module(module), attr))


def available_sources() -> dict[str, type[Source]]:
    """Name -> class for built-ins and third-party entry points.

    A third-party plug-in cannot replace a built-in name; one that fails to import is skipped
    with a log line (a broken plug-in must not break the CLI).
    """
    out: dict[str, type[Source]] = {name: _load(_BUILTIN_CLASSES[name]) for name in BUILTIN_NAMES}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        if ep.name in out:
            log.warning("source plug-in %s ignored: the name is built in", ep.name)
            continue
        try:
            out[ep.name] = cast(type[Source], ep.load())
        except Exception as exc:  # third-party code
            log.warning("source plug-in %s could not be loaded: %s", ep.name, type(exc).__name__)
    return out


def config_section(name: str, config: Config) -> object:
    """``config.sources.<name>`` or ``None`` (third-party sources have no section in the shape)."""
    return getattr(config.sources, name, None)


def get_source(
    name: str, config: Config, *, budget: Any | None = None, weekly: bool = False
) -> Source:
    """Instantiate a source with its config section; unknown name is a ``UsageError``.

    ``budget`` (the shared ``http.budget.Budget`` of the run's client) and ``weekly`` reach the
    OpenAlex source, which gates paid calls on them; other sources ignore them.
    """
    classes = available_sources()
    cls = classes.get(name)
    if cls is None:
        known = ", ".join(sorted(classes))
        raise UsageError(f"unknown source {name!r}; known sources: {known}")
    factory: Any = cls
    if name == "openalex":
        return cast(Source, factory(config_section(name, config), budget=budget, weekly=weekly))
    return cast(Source, factory(config_section(name, config)))


def enabled_sources(
    config: Config, *, budget: Any | None = None, weekly: bool = False
) -> list[Source]:
    """Sources with ``enabled: true`` in ``config.sources``.

    Order follows ``BUILTIN_NAMES``. Third-party sources have no config section in the fixed
    shape, so they are listed by ``available_sources`` but never enabled here. ``budget`` and
    ``weekly`` are handed to the OpenAlex source (see :func:`get_source`).
    """
    out: list[Source] = []
    for name in BUILTIN_NAMES:
        section = config_section(name, config)
        if section is not None and getattr(section, "enabled", False):
            out.append(get_source(name, config, budget=budget, weekly=weekly))
    return out


def host_intervals(config: Config) -> dict[str, float]:
    """Host -> minimum request interval for ``http.client.HostPolicy``.

    The configured ``min_interval_s`` of each source is applied to every host it contacts, never
    below the built-in floor. PubMed with an API key may go down to the keyed floor (0.1 s).
    """
    out: dict[str, float] = {}
    for name in BUILTIN_NAMES:
        source = get_source(name, config)
        section = config_section(name, config)
        floor = DEFAULT_MIN_INTERVAL_S.get(name, source.default_interval_s)
        interval = float(getattr(section, "min_interval_s", floor) or floor)
        if name == "pubmed" and getattr(section, "api_key_ref", None):
            floor = DEFAULT_MIN_INTERVAL_S["pubmed_keyed"]
            if interval == DEFAULT_MIN_INTERVAL_S["pubmed"]:
                interval = floor
        interval = max(interval, floor)
        for host in getattr(source, "hosts", ()) or ():
            out[host] = max(out.get(host, 0.0), interval)
    return out
