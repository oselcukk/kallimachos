"""``config`` commands: validate, show, migrate and document config.yaml.

validate | show | migrate | docs. Dead keys are errors (exit 2).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from kallimachos.cli_support import add_command, add_group
from kallimachos.config.load import (
    check_config_data,
    check_config_file,
    config_to_yaml,
    default_config_path,
    effective_config,
    load_config,
    read_yaml,
)
from kallimachos.config.migrate import CONFIG_MIGRATIONS, migrate
from kallimachos.config.schema import CONFIG_SCHEMA_VERSION, Config
from kallimachos.interfaces import SCHEMA_REGISTRY, ConfigError, ExitCode, json_schema
from kallimachos.state import store


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    g = add_group(
        sub, "config", help="validate, show, migrate or document the configuration", path="config"
    )
    p = add_command(
        g,
        "validate",
        cmd_validate,
        help="validate config.yaml (exit 2 on error)",
        path="config validate",
    )
    p.add_argument(
        "path", nargs="?", metavar="FILE", help="config file (default: <data_dir>/config.yaml)"
    )
    p = add_command(
        g, "show", cmd_show, help="print the effective configuration", path="config show"
    )
    p.add_argument(
        "path", nargs="?", metavar="FILE", help="config file (default: <data_dir>/config.yaml)"
    )
    p = add_command(
        g, "migrate", cmd_migrate, help="upgrade schema_version in place", path="config migrate"
    )
    p.add_argument(
        "path", nargs="?", metavar="FILE", help="config file (default: <data_dir>/config.yaml)"
    )
    p = add_command(
        g,
        "docs",
        cmd_docs,
        help="print the key, type and default of every config.yaml setting",
        path="config docs",
    )
    p.add_argument("--out", metavar="FILE", help="write the Markdown table here instead of stdout")


def _target_path(args: argparse.Namespace) -> tuple[Path, bool]:
    """(path, explicit): the FILE argument or ``<data_dir>/config.yaml``."""
    if getattr(args, "path", None):
        return Path(args.path).expanduser(), True
    return default_config_path(getattr(args, "data_dir", None)), False


def _emit(args: argparse.Namespace, payload: dict[str, object], lines: list[str]) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for line in lines:
            print(line)


def cmd_validate(args: argparse.Namespace) -> int:
    """Load + validate; print errors; exit 2 on failure."""
    path, explicit = _target_path(args)
    if not explicit and not path.exists():
        check = check_config_data({}, source="defaults")
        note = f"no config file at {path}; validating the defaults"
    else:
        check = check_config_file(path)
        note = str(path)
    report = check.report
    payload: dict[str, object] = {
        "path": str(path),
        "ok": report.ok,
        "errors": report.errors,
        "warnings": report.warnings,
        "migrations": check.migration_notes,
    }
    if not getattr(args, "json", False):
        for err in report.errors:
            print(f"error: {err}", file=sys.stderr)
        for warn in report.warnings:
            print(f"warning: {warn}", file=sys.stderr)
    lines = [f"{note}: {'valid' if report.ok else f'{len(report.errors)} error(s)'}"]
    if check.migration_notes:
        lines.append("pending migration: " + "; ".join(check.migration_notes))
    _emit(args, payload, lines)
    return int(ExitCode.OK if report.ok else ExitCode.USAGE)


def cmd_show(args: argparse.Namespace) -> int:
    """Print effective config (YAML, or JSON with --json); secrets stay as refs."""
    path, explicit = _target_path(args)
    config, layout = load_config(
        path if explicit else None, data_dir=getattr(args, "data_dir", None)
    )
    data = effective_config(config)
    if getattr(args, "json", False):
        print(
            json.dumps({"data_dir": str(layout.root), "config": data}, ensure_ascii=False, indent=2)
        )
    else:
        print(f"# effective configuration (data_dir: {layout.root})")
        print(config_to_yaml(data), end="")
    return int(ExitCode.OK)


def cmd_migrate(args: argparse.Namespace) -> int:
    """Apply config.migrate and rewrite the file atomically unless --dry-run.

    The previous file is kept as ``config.yaml.bak-v<old>`` before it is replaced. Comments in
    the YAML are not preserved by the rewrite; the backup keeps them.
    """
    path, _ = _target_path(args)
    data = read_yaml(path)
    old = data.get("schema_version", 1)
    migrated, notes = migrate(data, CONFIG_MIGRATIONS, CONFIG_SCHEMA_VERSION)
    if not notes:
        _emit(
            args,
            {"path": str(path), "changed": False, "schema_version": old},
            [f"{path}: already at schema_version {old}"],
        )
        return int(ExitCode.OK)
    check = check_config_data(migrated, source=str(path))
    if check.report.errors:
        raise ConfigError("migrated config is invalid: " + "; ".join(check.report.errors))
    dry = bool(getattr(args, "dry_run", False))
    backup = path.with_name(f"{path.name}.bak-v{old}")
    if not dry:
        shutil.copy2(path, backup)
        store.write_text_atomic(path, config_to_yaml(migrated))
    _emit(
        args,
        {
            "path": str(path),
            "changed": not dry,
            "dry_run": dry,
            "notes": notes,
            "backup": None if dry else str(backup),
        },
        [
            *notes,
            f"{path}: {'would be rewritten (dry run)' if dry else f'rewritten, backup {backup}'}",
        ],
    )
    return int(ExitCode.OK)


def _type_text(prop: dict[str, Any], defs: dict[str, Any]) -> str:
    if "$ref" in prop:
        return "section"
    if "anyOf" in prop:
        parts = [_type_text(p, defs) for p in prop["anyOf"]]
        return " | ".join(parts)
    if "enum" in prop:
        return " | ".join(f"`{v}`" for v in prop["enum"])
    if "const" in prop:
        return f"`{json.dumps(prop['const'])}` (fixed)"
    kind = prop.get("type")
    if kind == "array":
        inner = prop.get("items") or {}
        return f"list of {_type_text(inner, defs)}" if inner else "list"
    if (
        kind == "object"
        and "additionalProperties" in prop
        and isinstance(prop["additionalProperties"], dict)
    ):
        return f"mapping to {_type_text(prop['additionalProperties'], defs)}"
    return str(kind or "any")


def _constraints(prop: dict[str, Any]) -> str:
    bits = []
    for key in ("pattern", "minimum", "maximum", "minItems", "maxItems"):
        if key in prop:
            bits.append(f"{key} `{prop[key]}`")
    return "; ".join(bits)


def schema_rows(
    schema: dict[str, Any], defs: dict[str, Any], prefix: str = ""
) -> list[tuple[str, str, str, str]]:
    """``(key, type, default, description)`` for every leaf of the config schema, in field
    order; nested dataclasses are expanded with a dotted prefix."""
    rows: list[tuple[str, str, str, str]] = []
    for name, prop in schema.get("properties", {}).items():
        key = f"{prefix}{name}"
        ref = prop.get("$ref")
        if ref:
            target = defs[ref.rsplit("/", 1)[-1]]
            rows.append((key, "section", "", _doc(target)))
            rows.extend(schema_rows(target, defs, key + "."))
            continue
        default = json.dumps(prop["default"], ensure_ascii=False) if "default" in prop else ""
        description = "; ".join(x for x in (_doc(prop), _constraints(prop)) if x)
        rows.append((key, _type_text(prop, defs), default, description))
    return rows


def _doc(prop: dict[str, Any]) -> str:
    """The schema description unless it is only the dataclass's generated repr signature."""
    text = str(prop.get("description") or "").strip()
    if re.match(r"^[A-Z]\w*Config\(", text) or re.match(r"^[A-Z]\w*\(\w+: ", text):
        return ""
    return text


def render_config_docs() -> str:
    """Markdown table of every ``config.yaml`` key with type, default and description, generated
    from ``schemas/config.schema.json`` (same generator as the published file)."""
    schema = json_schema(Config, schema_id=SCHEMA_REGISTRY["config.schema.json"][1])
    defs = schema.get("$defs", {})
    lines = [
        "# config.yaml keys",
        "",
        f"Generated by `kallimachos config docs` from the configuration schema "
        f"(`schema_version` {CONFIG_SCHEMA_VERSION}). Unknown keys are errors (exit 2). Secrets "
        "live only in `*_ref` keys as `keyring:<name>` or `env:<VAR>` references.",
        "",
        "| Key | Type | Default | Description |",
        "|---|---|---|---|",
    ]
    for key, kind, default, description in schema_rows(schema, defs):
        cell_default = f"`{default}`" if default else ""
        cells = (
            f"`{key}`",
            kind.replace("|", "\\|"),
            cell_default,
            description.replace("|", "\\|"),
        )
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def cmd_docs(args: argparse.Namespace) -> int:
    """Print (or write with ``--out``) the key / type / default / description table of the
    configuration, generated from the schema so it never drifts from the code."""
    text = render_config_docs()
    out = getattr(args, "out", None)
    if out:
        if not getattr(args, "dry_run", False):
            store.write_text_atomic(Path(out).expanduser(), text)
        _emit(args, {"path": out, "rows": text.count("\n| `")}, [f"wrote {out}"])
        return int(ExitCode.OK)
    if getattr(args, "json", False):
        schema = json_schema(Config, schema_id=SCHEMA_REGISTRY["config.schema.json"][1])
        rows = schema_rows(schema, schema.get("$defs", {}))
        print(
            json.dumps(
                [{"key": k, "type": t, "default": d, "description": x} for k, t, d, x in rows],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        sys.stdout.write(text)
    return int(ExitCode.OK)
