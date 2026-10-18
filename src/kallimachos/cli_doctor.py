"""``doctor`` command: health report of the installation.

doctor [--fix]. Health report; --fix only repairs stale locks and missing directories.
"""

from __future__ import annotations

import argparse
import json

from kallimachos.cli_support import add_command
from kallimachos.config.load import load_config
from kallimachos.config.schema import Config
from kallimachos.doctor import run_doctor
from kallimachos.interfaces import ConfigError, ExitCode, dump
from kallimachos.paths import DataLayout, data_dir


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = add_command(sub, "doctor", cmd_doctor, help="health report", path="doctor")
    p.add_argument("--fix", action="store_true", help="safe repairs only")


def cmd_doctor(args: argparse.Namespace) -> int:
    """doctor.run_doctor; exit 0 when all checks pass, 1 otherwise.

    An invalid config does not stop the report: it shows up as a failing ``config`` check.
    ``--dry-run`` together with ``--fix`` reports what would be repaired without repairing.
    """
    override = getattr(args, "data_dir", None)
    config: Config | None
    try:
        config, layout = load_config(None, data_dir=override)
    except ConfigError:
        config, layout = None, DataLayout(data_dir(override))
    fix = bool(getattr(args, "fix", False)) and not bool(getattr(args, "dry_run", False))
    report = run_doctor(layout, config, fix=fix)
    if getattr(args, "json", False):
        payload = {"ok": report.ok, "data_dir": str(layout.root), "checks": dump(report.checks)}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"kallimachos doctor - data dir {layout.root}")
        for check in report.checks:
            mark = "fixed" if check.fixed else ("ok" if check.ok else "FAIL")
            print(f"  [{mark:>5}] {check.name}: {check.message}")
        print("all checks passed" if report.ok else "some checks failed")
    return int(ExitCode.OK if report.ok else ExitCode.UNEXPECTED)
