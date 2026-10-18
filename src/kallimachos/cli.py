"""Command-line entry point.

``cli.py`` only assembles the parser from the ``cli_<area>`` modules and maps exceptions to exit
codes. Command bodies live in the area modules (one per command group).

Exit codes: 0 success · 1 unexpected error · 2 config/profile invalid or usage error · 3 partial
success · 4 lock held · 5 approval required · 6 network/budget exhausted.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from kallimachos import (
    cli_archive,
    cli_bridge,
    cli_config,
    cli_doctor,
    cli_feedback,
    cli_hooks,
    cli_init,
    cli_notify,
    cli_process,
    cli_process_notes,
    cli_profile,
    cli_run,
    cli_schedule,
    cli_sources,
)
from kallimachos.cli_support import add_global_flags, add_group
from kallimachos.interfaces import ExitCode, KallimachosError
from kallimachos.version import __version__

PROG = "kallimachos"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Scan, filter, score, notify, archive and read research literature.",
        epilog=(
            "Exit codes: 0 ok, 1 unexpected error, 2 invalid config/profile or usage, "
            "3 partial success, 4 lock held, 5 approval required, 6 network/budget exhausted."
        ),
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    add_global_flags(parser)
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    cli_init.register(sub)
    cli_config.register(sub)
    cli_profile.register(sub)
    cli_sources.register(sub)
    cli_run.register(sub)
    cli_feedback.register(sub)
    cli_archive.register(sub)
    process_sub = add_group(
        sub,
        "process",
        help="read, card, check, note, links, cluster, queue, zotero",
        path="process",
    )
    cli_process.register(process_sub)
    cli_process_notes.register(process_sub)
    cli_bridge.register(sub)
    cli_notify.register(sub)
    cli_schedule.register(sub)
    cli_hooks.register(sub)
    cli_doctor.register(sub)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        # a bare group such as `kallimachos process` prints that group's help
        path = getattr(args, "command_path", None)
        if path:
            parser.parse_args([*path.split(), "--help"])
        parser.print_help(sys.stderr)
        return int(ExitCode.USAGE)
    try:
        return int(handler(args))
    except NotImplementedError as exc:
        detail = f" ({exc})" if str(exc) else ""
        print(f"{PROG}: '{args.command_path}' is not implemented yet{detail}", file=sys.stderr)
        return int(ExitCode.UNEXPECTED)
    except KallimachosError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return int(exc.exit_code)
    except KeyboardInterrupt:
        print(f"{PROG}: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
