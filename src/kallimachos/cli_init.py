"""``init`` command: first setup from the agent's answers file.

Wizard: writes config.yaml and a draft profile.yaml, runs doctor. Never overwrites without
--reconfigure (exit 5).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from kallimachos import doctor
from kallimachos.cli_support import add_command
from kallimachos.config import load as config_load
from kallimachos.config.schema import Config
from kallimachos.interfaces import ApprovalRequired, dump
from kallimachos.paths import DataLayout, data_dir
from kallimachos.profile.wizard import (
    ask_interactively,
    diff_existing,
    load_answers,
    materialize,
    write_yaml_validated,
)


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = add_command(sub, "init", cmd_init, help="interactive setup wizard", path="init")
    p.add_argument("--answers", metavar="FILE", help="answers.yaml written by the agent")
    p.add_argument("--lang", metavar="L", help="wizard/notification language (en, tr, ...)")
    p.add_argument(
        "--reconfigure", action="store_true", help="show the differences and ask for approval"
    )


def write_config_file(path: Path, config: Config) -> None:
    """Validated atomic YAML writer (``config.load.write_config``)."""
    config_load.write_config(path, config)


def cmd_init(args: argparse.Namespace) -> int:
    """Write config.yaml and the draft profile from the answers; exit 5 when existing files would
    change without approval."""
    answers: dict[str, Any]
    if args.answers:
        answers = load_answers(Path(args.answers))
        if args.lang:
            answers["language"] = args.lang
    else:
        answers = ask_interactively(args.lang or "en")
    override = args.data_dir or answers.get("data_dir")
    layout = DataLayout(data_dir(override))
    config, profile, notes = materialize(answers, layout)

    existing = [p for p in (layout.config_file, layout.profile_file) if p.exists()]
    diffs: list[str] = []
    if existing:
        names = ", ".join(str(p) for p in existing)
        if not args.reconfigure:
            raise ApprovalRequired(
                f"{names} already exist; run `init --reconfigure` to see the differences"
            )
        diffs = diff_existing(layout, config, profile)
        if not args.json:
            print("Differences against the existing files:", file=sys.stderr)
            for line in diffs or ["(none)"]:
                print(f"  {line}", file=sys.stderr)
        if not args.yes:
            raise ApprovalRequired(
                "existing files would be rewritten; re-run with --yes after the user approved "
                "the differences above"
            )

    written: list[str] = []
    if not args.dry_run:
        layout.ensure_private_dirs()
        write_config_file(layout.config_file, config)
        written.append(str(layout.config_file))
        write_yaml_validated(layout.profile_file, dump(profile), "profile.schema.json")
        written.append(str(layout.profile_file))
        report = doctor.run_doctor(layout, config)
        notes.append("doctor: ok" if report.ok else "doctor: see `kallimachos doctor`")
    else:
        notes.append("dry run: nothing was written")

    if args.json:
        print(
            json.dumps(
                {
                    "data_dir": str(layout.root),
                    "written": written,
                    "profile_status": profile.status,
                    "channels": [c.id for c in profile.channels],
                    "notes": notes,
                    "differences": diffs,
                },
                ensure_ascii=False,
            )
        )
    else:
        print(f"Data directory: {layout.root}")
        for path in written:
            print(f"  wrote {path}")
        for note in notes:
            print(f"  note: {note}")
        print(
            "Next: review `kallimachos profile show`, then `kallimachos profile approve` "
            "(no scan runs before approval)."
        )
    return 0
