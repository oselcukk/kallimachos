"""``sources`` commands: list the sources and test them.

list | test [NAME]. `test` makes one small network request per source (every enabled source
when no name is given; exit 6 when any of them fails).

Secrets are never printed: ``list`` reports each key reference as present/absent only.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from kallimachos.cli_support import add_command, add_group
from kallimachos.config.schema import Config
from kallimachos.interfaces import ExitCode, HttpClient, dump
from kallimachos.paths import DataLayout


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    g = add_group(
        sub, "sources", help="list sources or test one with a single request", path="sources"
    )
    add_command(g, "list", cmd_list, help="endpoints, intervals, key status", path="sources list")
    p = add_command(
        g, "test", cmd_test, help="one small request against a source", path="sources test"
    )
    p.add_argument("name", nargs="?", metavar="NAME", help="a source; default: every enabled one")


def _load(args: argparse.Namespace) -> tuple[Config, DataLayout]:
    from kallimachos.config.load import load_config

    return load_config(None, data_dir=getattr(args, "data_dir", None))


def _key_status(ref: str | None) -> str:
    if not ref:
        return "not set"
    from kallimachos.secrets import is_present

    return "present" if is_present(ref) else "absent"


def build_http(config: Config, layout: DataLayout) -> HttpClient:
    """The shared HTTP client (``http.client.from_config``: registry intervals, cache, budget)."""
    from kallimachos.http.client import from_config

    return from_config(config, layout)


def source_rows(config: Config) -> list[dict[str, Any]]:
    from kallimachos.sources.base import BaseSource
    from kallimachos.sources.registry import available_sources, config_section, get_source

    rows: list[dict[str, Any]] = []
    for name in available_sources():
        source = get_source(name, config)
        section = config_section(name, config)
        refs = source.key_refs() if isinstance(source, BaseSource) else {}
        rows.append(
            {
                "name": name,
                "enabled": bool(getattr(section, "enabled", False)),
                "min_interval_s": float(
                    getattr(section, "min_interval_s", source.default_interval_s)
                ),
                "needs_key": source.needs_key,
                "hosts": list(getattr(source, "hosts", ()) or ()),
                "keys": {label: _key_status(ref) for label, ref in refs.items()},
            }
        )
    return rows


def cmd_list(args: argparse.Namespace) -> int:
    """Table of registered sources with enabled/interval/key presence."""
    config, _layout = _load(args)
    rows = source_rows(config)
    if getattr(args, "json", False):
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return int(ExitCode.OK)
    header = f"{'source':<18} {'enabled':<8} {'interval':>8}  {'keys':<34} hosts"
    print(header)
    for r in rows:
        keys = ", ".join(f"{k}: {v}" for k, v in r["keys"].items()) or "-"
        print(
            f"{r['name']:<18} {'yes' if r['enabled'] else 'no':<8} {r['min_interval_s']:>7.2f}s"
            f"  {keys:<34} {', '.join(r['hosts']) or '-'}"
        )
    return int(ExitCode.OK)


def cmd_test(args: argparse.Namespace) -> int:
    """Call ``Source.test()`` for the named source, or for every enabled source; exit 6 when at
    least one request fails, 2 when nothing is enabled."""
    from kallimachos.interfaces import UsageError
    from kallimachos.sources.registry import enabled_sources, get_source

    config, layout = _load(args)
    name = getattr(args, "name", None)
    sources = [get_source(name, config)] if name else enabled_sources(config)
    if not sources:
        raise UsageError("no source is enabled in config.yaml (sources.<name>.enabled)")
    http = build_http(config, layout)
    results = [(source.name, source.test(http)) for source in sources]
    if getattr(args, "json", False):
        payload = {src: dump(result) for src, result in results}
        print(json.dumps(payload if not name else payload[name], ensure_ascii=False, indent=2))
    else:
        for src, result in results:
            status = "ok" if result.ok else "FAILED"
            latency = f" ({result.latency_s:.2f}s)" if result.latency_s is not None else ""
            http_status = f" HTTP {result.http_status}" if result.http_status is not None else ""
            stream = sys.stdout if result.ok else sys.stderr
            print(f"{src}: {status}{http_status}{latency}: {result.message}", file=stream)
            if result.key_present is not None:
                print(f"  key: {'present' if result.key_present else 'absent'}", file=stream)
    return int(ExitCode.OK if all(r.ok for _s, r in results) else ExitCode.EXHAUSTED)
