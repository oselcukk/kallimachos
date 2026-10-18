"""Health report: base checks plus the schedule and hook extension checks.

Checks: config/profile validity, secret presence (present/absent only), locks (stale?), disk
space, last run, source errors, state consistency, Avenox detection and the literature folder
being indexed (not in ``excluded_dirs``). ``--fix`` performs only safe repairs: expired locks and
missing directories.

Nothing here prints or returns a secret value; secret checks say ``present`` or ``absent``.
A missing data directory or missing sub-directories are not failures (writers create them on
demand), so ``doctor`` is clean on a fresh machine.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kallimachos.config.load import check_config_file, read_yaml
from kallimachos.config.schema import Config
from kallimachos.interfaces import (
    CatalogEntry,
    ConfigError,
    FeedbackEntry,
    KallimachosError,
    SchemaViolation,
    SeenEntry,
    StatusEntry,
    Weights,
    build,
    dump,
)
from kallimachos.paths import DataLayout, tighten_mode, too_open
from kallimachos.secrets import is_present
from kallimachos.state import store
from kallimachos.state.lock import FileLock, read_lock_info
from kallimachos.state.runs import list_run_ids, load_run, run_failed

MIN_FREE_BYTES = 500 * 1024 * 1024
"""Below this much free space on the data volume ``doctor`` reports a problem."""


@dataclass
class DoctorCheck:
    name: str
    ok: bool
    message: str = ""
    fixable: bool = False
    fixed: bool = False


@dataclass
class DoctorReport:
    checks: list[DoctorCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok or c.fixed for c in self.checks)


# ---------------------------------------------------------------------------
# individual checks
# ---------------------------------------------------------------------------


def _check_dirs(layout: DataLayout, fix: bool) -> DoctorCheck:
    missing = [p for p in [layout.root, *layout.required_dirs()] if not p.is_dir()]
    if not missing:
        return DoctorCheck("directories", True, f"all present under {layout.root}")
    if fix:
        layout.ensure_private_dirs()
        return DoctorCheck(
            "directories", True, f"created {len(missing)} missing", fixable=True, fixed=True
        )
    return DoctorCheck(
        "directories",
        True,
        f"{len(missing)} missing (created on demand; `doctor --fix` creates them now)",
        fixable=True,
    )


def _check_config(layout: DataLayout, config: Config | None) -> tuple[DoctorCheck, Config | None]:
    path = layout.config_file
    if not path.exists():
        msg = "no config.yaml; defaults in use (run `kallimachos init`)"
        return DoctorCheck("config", True, msg), config or Config()
    check = check_config_file(path)
    if check.report.errors:
        return DoctorCheck("config", False, "; ".join(check.report.errors)), config
    msg = "valid"
    if check.report.warnings:
        msg += " (warnings: " + "; ".join(check.report.warnings) + ")"
    if check.migration_notes:
        msg += " (run `kallimachos config migrate`)"
    return DoctorCheck("config", True, msg), check.config


def _check_profile(layout: DataLayout) -> DoctorCheck:
    from kallimachos.profile.schema import Profile

    path = layout.profile_file
    if not path.exists():
        return DoctorCheck("profile", True, "no profile.yaml yet (run `kallimachos init`)")
    try:
        profile = build(Profile, read_yaml(path))
    except (ConfigError, SchemaViolation) as exc:
        return DoctorCheck("profile", False, str(exc))
    if not profile.is_approved:
        return DoctorCheck(
            "profile", True, "valid, status draft (run `kallimachos profile approve`)"
        )
    return DoctorCheck("profile", True, f"valid, approved, {len(profile.channels)} channel(s)")


def _refs(data: Any, path: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(data, dict):
        for k, v in data.items():
            sub = f"{path}.{k}" if path else str(k)
            if str(k).endswith("_ref"):
                if isinstance(v, str) and v:
                    out.append((sub, v))
            else:
                out.extend(_refs(v, sub))
    return out


def _check_secrets(config: Config | None) -> DoctorCheck:
    if config is None:
        return DoctorCheck("secrets", True, "skipped (config invalid)")
    refs = _refs(dump(config))
    if not refs:
        return DoctorCheck("secrets", True, "no secret references configured")
    states = [(where, is_present(ref)) for where, ref in refs]
    absent = [where for where, present in states if not present]
    parts = [f"{where}: {'present' if present else 'absent'}" for where, present in states]
    oa = config.sources.openalex
    if oa.enabled and not oa.api_key_ref and oa.daily_budget_usd > 0.08:
        parts.append(
            "openalex: no API key; the free allowance is about $0.10/day, consider "
            "daily_budget_usd <= 0.08"
        )
    return DoctorCheck("secrets", not absent, "; ".join(parts))


def _check_locks(layout: DataLayout, fix: bool) -> DoctorCheck:
    if not layout.locks.is_dir():
        return DoctorCheck("locks", True, "no locks")
    stale: list[Path] = []
    held: list[str] = []
    for path in sorted(layout.locks.glob("*.lock")):
        lock = FileLock(path)
        if lock.is_stale():
            stale.append(path)
        else:
            info = read_lock_info(path) or {}
            held.append(f"{path.stem} (pid {info.get('pid', '?')})")
    if not stale:
        msg = "held: " + ", ".join(held) if held else "no locks"
        return DoctorCheck("locks", True, msg)
    names = ", ".join(p.stem for p in stale)
    if fix:
        for p in stale:
            p.unlink(missing_ok=True)
        return DoctorCheck("locks", True, f"removed stale: {names}", fixable=True, fixed=True)
    return DoctorCheck(
        "locks", False, f"stale: {names} (`doctor --fix` removes them)", fixable=True
    )


def _existing_ancestor(path: Path) -> Path:
    p = path
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def _check_disk(layout: DataLayout) -> DoctorCheck:
    try:
        usage = shutil.disk_usage(_existing_ancestor(layout.root))
    except OSError as exc:
        return DoctorCheck("disk", False, f"cannot read disk usage: {exc.strerror}")
    free_mb = usage.free // (1024 * 1024)
    if usage.free < MIN_FREE_BYTES:
        return DoctorCheck("disk", False, f"only {free_mb} MB free on the data volume")
    return DoctorCheck("disk", True, f"{free_mb} MB free")


def _check_last_run(layout: DataLayout) -> list[DoctorCheck]:
    ids = list_run_ids(layout)
    if not ids:
        return [DoctorCheck("last run", True, "no runs yet")]
    rid = ids[-1]
    try:
        run = load_run(layout, rid)
    except KallimachosError as exc:
        return [DoctorCheck("last run", False, f"{rid}: unreadable ({exc})")]
    checks = [
        DoctorCheck(
            "last run",
            not run_failed(run),
            f"{rid}: {len(run.candidates)} candidate(s)" + (", failed" if run_failed(run) else ""),
        )
    ]
    failing = {name: s.errors for name, s in run.sources.items() if s.errors}
    if failing:
        detail = "; ".join(f"{name}: {errs[0]}" for name, errs in sorted(failing.items()))
        checks.append(DoctorCheck("source errors", False, f"{rid}: {detail}"))
    else:
        checks.append(DoctorCheck("source errors", True, f"{rid}: none"))
    return checks


_JSONL_SHAPES: list[tuple[str, Callable[[DataLayout], Path], type[Any]]] = [
    ("seen", lambda lay: lay.seen_file, SeenEntry),
    ("feedback", lambda lay: lay.feedback_file, FeedbackEntry),
    ("catalog", lambda lay: lay.catalog_file, CatalogEntry),
    ("status", lambda lay: lay.status_file, StatusEntry),
]


def _check_state(layout: DataLayout) -> DoctorCheck:
    problems: list[str] = []
    counted: list[str] = []
    for name, locate, cls in _JSONL_SHAPES:
        path = locate(layout)
        if not path.exists():
            continue
        n = 0
        try:
            for n, line in enumerate(store.read_jsonl(path), start=1):
                build(cls, line, path=f"{path.name}:{n}")
        except SchemaViolation as exc:
            problems.append(str(exc))
            continue
        counted.append(f"{name} {n}")
    weights = layout.weights_file
    if weights.exists():
        try:
            build(Weights, store.read_json(weights), path=weights.name)
            counted.append("weights ok")
        except SchemaViolation as exc:
            problems.append(str(exc))
    if problems:
        return DoctorCheck("state", False, "; ".join(problems))
    return DoctorCheck("state", True, ", ".join(counted) if counted else "empty")


def _private_paths(layout: DataLayout) -> list[tuple[Path, int]]:
    """``(path, allowed_mode)`` for the data directory, its subdirectories and the files that
    hold state, config, cached responses and the profile (owner-only on POSIX)."""
    out: list[tuple[Path, int]] = []
    if layout.root.is_dir():
        out.append((layout.root, 0o700))
    for directory in layout.required_dirs():
        if directory.is_dir():
            out.append((directory, 0o700))
    for directory in (layout.state, layout.runs, layout.http_cache, layout.budget_dir):
        if directory.is_dir():
            out.extend((f, 0o600) for f in directory.iterdir() if f.is_file())
    for f in (layout.config_file, layout.profile_file, layout.weights_file):
        if f.is_file():
            out.append((f, 0o600))
    return out


def _check_permissions(layout: DataLayout, fix: bool) -> DoctorCheck:
    """POSIX only: the data directory tree is ``0700`` and its files ``0600``; ``--fix`` tightens
    them (never loosens)."""
    if sys.platform.startswith("win"):
        return DoctorCheck("permissions", True, "not checked on Windows")
    if not layout.config_file.exists():
        return DoctorCheck("permissions", True, "no installation yet (init sets 0700/0600)")
    open_paths = [(p, m) for p, m in _private_paths(layout) if too_open(p, m)]
    if not open_paths:
        return DoctorCheck("permissions", True, "data directory and files are owner-only")
    if fix:
        changed = sum(1 for p, m in open_paths if tighten_mode(p, m))
        return DoctorCheck(
            "permissions",
            changed == len(open_paths),
            f"tightened {changed} of {len(open_paths)} path(s) to owner-only",
            fixable=True,
            fixed=changed == len(open_paths),
        )
    sample = ", ".join(str(p.relative_to(layout.root) or ".") for p, _m in open_paths[:3])
    return DoctorCheck(
        "permissions",
        False,
        f"{len(open_paths)} path(s) readable by other users ({sample}...); "
        "`doctor --fix` sets 0700/0600",
        fixable=True,
    )


def _check_cache_secrets(layout: DataLayout, fix: bool) -> DoctorCheck:
    """HTTP cache entries written before the URL redaction rule may still carry ``api_key`` /
    ``email`` query values on disk; ``--fix`` deletes them (the cache is only a cache)."""
    from kallimachos.http.cache import Cache

    found = Cache(layout.http_cache).entries_with_secrets()
    if not found:
        return DoctorCheck("cache secrets", True, "no cached URL carries a key or an e-mail")
    if fix:
        removed = 0
        for path in found:
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return DoctorCheck(
            "cache secrets",
            removed == len(found),
            f"removed {removed} cache entr{'y' if removed == 1 else 'ies'} with secret values",
            fixable=True,
            fixed=removed == len(found),
        )
    return DoctorCheck(
        "cache secrets",
        False,
        f"{len(found)} cache entr{'y' if len(found) == 1 else 'ies'} store a URL with a key or "
        f"e-mail (written by an older version); `doctor --fix` deletes them",
        fixable=True,
    )


def _check_avenox(config: Config | None) -> DoctorCheck | None:
    if config is None or (config.notes.target != "avenox" and not config.notes.vault_path):
        return None
    from kallimachos.process import avenox

    word = next(
        (
            part
            for part in Path(config.notes.literature_folder).parts
            if part not in ("", ".", "/") and avenox.sensitive_name(part)
        ),
        None,
    )
    if word is not None:
        return DoctorCheck(
            "avenox",
            False,
            f"literature folder {config.notes.literature_folder!r} contains {word!r}, a word "
            "Avenox keeps out of its index; notes there would not be found",
        )
    vault = Path(config.notes.vault_path or "").expanduser()
    if not config.notes.vault_path or not vault.is_dir():
        ok = config.notes.target != "avenox"
        return DoctorCheck("avenox", ok, f"vault {vault} not found")
    info = avenox.detect(vault)
    if info is None:
        ok = config.notes.target != "avenox"
        return DoctorCheck("avenox", ok, "not detected in the vault (plain folder)")
    indexed, why = avenox.literature_folder_indexed(info, config.notes.literature_folder)
    msg = f"detected{' ' + info.version if info.version else ''}; literature folder: {why}"
    return DoctorCheck("avenox", indexed, msg)


# ---------------------------------------------------------------------------

EXTENSION_MODULES: tuple[str, ...] = ("kallimachos.schedule", "kallimachos.hooks")
"""Extension modules exposing ``doctor_checks(layout, config) -> list[DoctorCheck]`` (imported
lazily). A missing module or function contributes nothing; one that raises becomes a failing
check named after it (a broken extension must not hide the base report)."""


def extension_checks(layout: DataLayout, config: Config | None) -> list[DoctorCheck]:
    import importlib

    out: list[DoctorCheck] = []
    for name in EXTENSION_MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        checks = getattr(module, "doctor_checks", None)
        if not callable(checks):
            continue
        short = name.rsplit(".", 1)[-1]
        try:
            result = checks(layout, config)
        except NotImplementedError:
            continue  # the module provides no checks yet
        except Exception as exc:  # extension code must not break the report
            out.append(DoctorCheck(short, False, f"check failed: {type(exc).__name__}: {exc}"))
            continue
        out.extend(c for c in result if isinstance(c, DoctorCheck))
    return out


def run_doctor(layout: DataLayout, config: Config | None, *, fix: bool = False) -> DoctorReport:
    """Run every check; with ``fix`` repair what is safe. The schedule and hook
    checks are appended through :func:`extension_checks`."""
    report = DoctorReport()
    report.checks.append(_check_dirs(layout, fix))
    config_check, effective = _check_config(layout, config)
    report.checks.append(config_check)
    report.checks.append(_check_profile(layout))
    report.checks.append(_check_secrets(effective))
    report.checks.append(_check_locks(layout, fix))
    report.checks.append(_check_disk(layout))
    report.checks.extend(_check_last_run(layout))
    report.checks.append(_check_state(layout))
    report.checks.append(_check_cache_secrets(layout, fix))
    report.checks.append(_check_permissions(layout, fix))
    avenox_check = _check_avenox(effective)
    if avenox_check is not None:
        report.checks.append(avenox_check)
    # Scheduling and hook checks judge an *installation*: before config.yaml exists there is
    # nothing to install, and ``doctor`` stays clean on a fresh machine (L1 acceptance).
    installed = effective if layout.config_file.exists() else None
    report.checks.extend(extension_checks(layout, installed))
    return report
