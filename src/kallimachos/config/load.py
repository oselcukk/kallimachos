"""Load ``config.yaml`` into :class:`~kallimachos.config.schema.Config`.

Order: read YAML -> ``migrate`` to the current ``schema_version`` -> strict ``build`` (dead key =
exit 2) -> ``validate`` (semantic checks) -> resolve ``data_dir``.

Data directory precedence: explicit ``--data-dir`` > ``data_dir`` in the config file >
``KALLIMACHOS_HOME`` > OS default (``paths.data_dir``). When no config path is given the file is
looked up in the directory chosen by ``--data-dir`` / ``KALLIMACHOS_HOME`` / OS default.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kallimachos.config.migrate import CONFIG_MIGRATIONS, migrate
from kallimachos.config.schema import CONFIG_SCHEMA_VERSION, Config
from kallimachos.config.validate import ValidationReport, validate_config
from kallimachos.interfaces import ConfigError, SchemaViolation, build, dump
from kallimachos.paths import DataLayout
from kallimachos.paths import data_dir as resolve_data_dir
from kallimachos.state import store

log = logging.getLogger("kallimachos.config")


def read_yaml(path: Path) -> dict[str, Any]:
    """``yaml.safe_load``; a non-mapping document is a :class:`ConfigError`.

    An empty document is an empty mapping; a missing file or a YAML syntax error is a
    :class:`ConfigError` as well.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"{path}: file not found") from None
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read ({exc.strerror})") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark is not None else ""
        raise ConfigError(f"{path}: invalid YAML{where}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping, got {type(data).__name__}")
    return {str(k): v for k, v in data.items()}


@dataclass
class ConfigCheck:
    """Outcome of :func:`check_config_data`: never raises for content problems."""

    config: Config | None
    report: ValidationReport = field(default_factory=ValidationReport)
    migration_notes: list[str] = field(default_factory=list)


def check_config_data(data: dict[str, Any], *, source: str = "config.yaml") -> ConfigCheck:
    """Migrate, build and validate a raw mapping, collecting every problem."""
    report = ValidationReport()
    try:
        migrated, notes = migrate(data, CONFIG_MIGRATIONS, CONFIG_SCHEMA_VERSION)
    except ConfigError as exc:
        report.errors.append(f"{source}: {exc}")
        return ConfigCheck(None, report)
    try:
        config = build(Config, migrated)
    except SchemaViolation as exc:
        report.errors.append(f"{source}: {exc}")
        return ConfigCheck(None, report, notes)
    semantic = validate_config(config)
    report.errors.extend(semantic.errors)
    report.warnings.extend(semantic.warnings)
    return ConfigCheck(config, report, notes)


def check_config_file(path: Path) -> ConfigCheck:
    """:func:`check_config_data` for a file; unreadable YAML becomes a report error."""
    try:
        data = read_yaml(path)
    except ConfigError as exc:
        return ConfigCheck(None, ValidationReport(errors=[str(exc)]))
    return check_config_data(data, source=str(path))


def default_config_path(data_dir: str | None = None) -> Path:
    return DataLayout(resolve_data_dir(data_dir)).config_file


def load_config(
    path: Path | None = None, *, data_dir: str | None = None
) -> tuple[Config, DataLayout]:
    """Load, migrate, build and validate. ``path=None`` means ``<data_dir>/config.yaml``; a missing
    file yields the defaults.

    Raises :class:`ConfigError` (exit 2) listing every error; warnings go to the log.
    """
    if path is None:
        path = default_config_path(data_dir)
        data = read_yaml(path) if path.exists() else {}
    else:
        data = read_yaml(path)
    check = check_config_data(data, source=str(path))
    if check.config is None or check.report.errors:
        raise ConfigError("; ".join(check.report.errors))
    for warning in check.report.warnings:
        log.warning("%s", warning)
    config = check.config
    root = resolve_data_dir(data_dir or config.data_dir)
    return config, DataLayout(root)


def effective_config(config: Config) -> dict[str, Any]:
    """Plain mapping of the effective configuration for ``config show`` (secrets stay as refs)."""
    data = dump(config)
    assert isinstance(data, dict)
    return data


def config_to_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)


def update_config_values(path: Path, config: Config, changes: Mapping[str, Any]) -> Path | None:
    """Set dotted keys (``notify.email.confirmed_at``) in an existing ``config.yaml`` in place,
    keeping the user's comments and layout, after writing a backup (``config.yaml.bak-<stamp>``).
    ``config`` must already carry the new values; it is what the file is checked against. When
    the file does not exist or the targeted edit is not possible, the whole file is written.
    Returns the backup path (``None`` when nothing was backed up)."""
    from kallimachos.config.yaml_edit import set_scalars

    report = validate_config(config)
    if report.errors:
        raise ConfigError("; ".join(report.errors))
    backup = store.backup_copy(path) if path.exists() else None
    if path.exists():
        text = path.read_text(encoding="utf-8")
        edited = set_scalars(text, {tuple(k.split(".")): v for k, v in changes.items()})
        if edited is not None:
            try:
                loaded = yaml.safe_load(edited) or {}
                check = check_config_data(loaded, source=str(path)).config
            except (ConfigError, SchemaViolation, yaml.YAMLError):
                check = None
            if check is not None and dump(check) == dump(config):
                store.write_text_atomic(path, edited)
                return backup
    write_config(path, config)
    return backup


def write_config(path: Path, config: Config) -> None:
    """Serialise ``config`` as YAML atomically (used by ``init``).

    The configuration is validated first; a config with errors is never written.
    """
    report = validate_config(config)
    if report.errors:
        raise ConfigError("; ".join(report.errors))
    header = (
        "# Kallimachos configuration (written by kallimachos). Secrets never live here:\n"
        "# use keyring:<name> or env:<VAR> references in *_ref keys.\n"
    )
    store.write_text_atomic(path, header + config_to_yaml(effective_config(config)))
