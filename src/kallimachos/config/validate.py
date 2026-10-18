"""Semantic validation of a built :class:`Config`.

Checks (each failure is a :class:`~kallimachos.interfaces.ConfigError`, exit 2):

* ``sources.<name>.min_interval_s`` >= ``DEFAULT_MIN_INTERVAL_S[name]``;
* any string that :func:`kallimachos.secrets.looks_like_secret` outside ``*_ref`` keys;
* ``*_ref`` values are ``keyring:`` or ``env:`` references;
* ``notes.target: avenox`` requires ``notes.vault_path/beyin.py``;
* ``notes.literature_folder`` must not be ``notes`` or ``knowledge`` (Avenox territory: warning);
* ``timezone`` is a known IANA zone when set; ``schedule.*_at`` is HH:MM.
"""

from __future__ import annotations

import re
import zoneinfo
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from kallimachos.config.schema import DEFAULT_MIN_INTERVAL_S, Config
from kallimachos.interfaces import dump
from kallimachos.secrets import REF_RE, looks_like_secret

AVENOX_RESERVED_FOLDERS = frozenset({"notes", "knowledge"})
_HHMM_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _is_ref_key(key: str) -> bool:
    return key.endswith("_ref")


def find_secret_like_values(data: object, path: str = "") -> list[str]:
    """Dotted paths of values that look like secrets and are not under a ``*_ref`` key."""
    found: list[str] = []
    if isinstance(data, Mapping):
        for key, value in data.items():
            k = str(key)
            sub = f"{path}.{k}" if path else k
            if _is_ref_key(k):
                continue
            found.extend(find_secret_like_values(value, sub))
    elif isinstance(data, list | tuple):
        for i, value in enumerate(data):
            found.extend(find_secret_like_values(value, f"{path}[{i}]"))
    elif isinstance(data, str) and looks_like_secret(data):
        found.append(path or "<root>")
    return found


def _ref_values(data: object, path: str = "") -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    if isinstance(data, Mapping):
        for key, value in data.items():
            k = str(key)
            sub = f"{path}.{k}" if path else k
            if _is_ref_key(k):
                out.append((sub, value))
            else:
                out.extend(_ref_values(value, sub))
    elif isinstance(data, list | tuple):
        for i, value in enumerate(data):
            out.extend(_ref_values(value, f"{path}[{i}]"))
    return out


def _timezone_known(name: str) -> bool | None:
    """True/False, or ``None`` when no time-zone database is installed (Windows without
    ``tzdata``); then the check is downgraded to a warning."""
    try:
        zoneinfo.ZoneInfo(name)
    except zoneinfo.ZoneInfoNotFoundError:
        return None if not zoneinfo.available_timezones() else False
    except (ValueError, OSError):
        return False
    return True


def validate_config(config: Config) -> ValidationReport:
    """Collect every problem; the caller raises ``ConfigError`` when ``errors`` is non-empty."""
    report = ValidationReport()
    plain = dump(config)

    # 1. interval floors (permanent, not configurable)
    for name, floor in DEFAULT_MIN_INTERVAL_S.items():
        source = getattr(config.sources, name, None)
        if source is None:
            continue
        if name == "pubmed" and getattr(source, "api_key_ref", None):
            floor = DEFAULT_MIN_INTERVAL_S.get("pubmed_keyed", floor)
        interval = float(source.min_interval_s)
        if interval < floor:
            report.errors.append(
                f"sources.{name}.min_interval_s = {interval} is below the permanent floor {floor}"
            )

    # 2. secrets only as references
    for where in find_secret_like_values(plain):
        report.errors.append(
            f"{where}: looks like a secret; store it in the keyring or the environment and "
            "reference it from a *_ref key (keyring:<name> or env:<VAR>)"
        )
    for where, ref_value in _ref_values(plain):
        if ref_value is None:
            continue
        if not isinstance(ref_value, str) or not REF_RE.match(ref_value):
            report.errors.append(
                f"{where}: must be a reference 'keyring:<name>' or 'env:<VAR>', not a value"
            )

    # 3. notes target
    notes = config.notes
    if notes.target == "avenox":
        if not notes.vault_path:
            report.errors.append("notes.target is 'avenox' but notes.vault_path is not set")
        else:
            beyin = Path(notes.vault_path).expanduser() / "beyin.py"
            if not beyin.is_file():
                report.errors.append(
                    f"notes.target is 'avenox' but {beyin} does not exist "
                    "(install Avenox there or use notes.target: plain)"
                )
    folder = notes.literature_folder.strip().strip("/\\")
    first = re.split(r"[/\\]", folder)[0].lower() if folder else ""
    if not folder:
        report.errors.append("notes.literature_folder must not be empty")
    elif first in AVENOX_RESERVED_FOLDERS:
        report.warnings.append(
            f"notes.literature_folder '{notes.literature_folder}' is inside Avenox's "
            f"'{first}/' area; use a separate folder such as 'Literature'"
        )
    if Path(folder).is_absolute() or ".." in re.split(r"[/\\]", folder):
        report.errors.append("notes.literature_folder must be a relative path inside the vault")

    # 4. timezone and schedule times
    if config.timezone:
        known = _timezone_known(config.timezone)
        if known is False:
            report.errors.append(f"timezone {config.timezone!r} is not a known IANA zone")
        elif known is None:
            report.warnings.append(
                f"timezone {config.timezone!r} could not be checked (no time-zone database; "
                "install the 'tzdata' package)"
            )
    for key in ("daily_at", "weekly_at", "archive_at"):
        at = str(getattr(config.schedule, key))
        if not _HHMM_RE.match(at):
            report.errors.append(f"schedule.{key} = {at!r} is not a valid HH:MM time")

    # 5. a few value ranges the shape cannot express
    lo, hi = config.feedback.weights.clamp
    if not 0 < lo <= 1 <= hi:
        report.errors.append(
            f"feedback.weights.clamp = [{lo}, {hi}] must satisfy 0 < lo <= 1 <= hi"
        )
    if config.language not in ("en", "tr"):
        report.warnings.append(
            f"language {config.language!r} has no message catalog; English is used"
        )
    if config.notify.email.confirmed_at is None and "email" in config.notify.channels:
        report.warnings.append(
            "notify.email is not confirmed yet; run `kallimachos notify confirm-email`"
        )
    return report
