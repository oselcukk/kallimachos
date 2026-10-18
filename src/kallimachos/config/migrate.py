"""``schema_version`` migrations for config.yaml and profile.yaml."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from kallimachos.interfaces import ConfigError

Migration = Callable[[dict[str, Any]], dict[str, Any]]

CONFIG_MIGRATIONS: dict[int, Migration] = {}
"""``from_version -> function`` producing ``from_version + 1``. Empty while schema_version == 1."""

PROFILE_MIGRATIONS: dict[int, Migration] = {}


def migrate(
    data: dict[str, Any], migrations: dict[int, Migration], target: int
) -> tuple[dict[str, Any], list[str]]:
    """Apply migrations from ``data["schema_version"]`` up to ``target``; return (data, notes).
    A version above ``target`` is a :class:`ConfigError`.

    A missing ``schema_version`` is read as ``1``. The input mapping is not modified. Every step
    sets ``schema_version`` itself, so a migration function only reshapes the content.
    """
    raw = data.get("schema_version", 1)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ConfigError(f"schema_version must be a positive integer, got {raw!r}")
    if raw > target:
        raise ConfigError(
            f"schema_version {raw} is newer than this kallimachos supports ({target}); upgrade"
        )
    out = copy.deepcopy(data)
    notes: list[str] = []
    if "schema_version" not in data:
        notes.append(f"schema_version missing; assumed 1, set to {target}")
    version = raw
    while version < target:
        step = migrations.get(version)
        if step is None:
            raise ConfigError(f"no migration from schema_version {version} to {version + 1}")
        out = step(copy.deepcopy(out))
        version += 1
        out["schema_version"] = version
        notes.append(f"schema_version {version - 1} -> {version}")
    out["schema_version"] = version
    return out, notes
