"""Read ``profile.yaml`` into :class:`~kallimachos.profile.schema.Profile` (one loader for
``run``, ``archive``, ``process``, ``profile show/approve``)."""

from __future__ import annotations

from pathlib import Path

from kallimachos.config.load import read_yaml
from kallimachos.interfaces import ConfigError, SchemaViolation, UsageError, build
from kallimachos.profile.schema import Profile


def load_profile(path: Path, *, missing_ok: bool = False) -> Profile:
    """Strict build of the YAML file. A missing file is a :class:`UsageError` (exit 2) pointing at
    ``init`` / ``profile build``, or the empty profile when ``missing_ok``; an invalid file is a
    :class:`ConfigError`."""
    if not path.is_file():
        if missing_ok:
            return Profile()
        raise UsageError(
            f"no profile at {path}; run `kallimachos init` or `kallimachos profile build`"
        )
    try:
        return build(Profile, read_yaml(path), path=path.name)
    except SchemaViolation as exc:
        raise ConfigError(f"{path}: {exc}") from None
