"""Helpers shared by ``cli.py`` and the ``cli_<area>.py`` modules.

Every area module exposes ``register(sub)`` and handler functions ``cmd_*(args) -> int``.
``cli.py`` only builds the parser and dispatches. A handler that is not implemented yet raises
``NotImplementedError``; ``cli.main`` turns that into exit code 1 with a clear message.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Callable, Sequence
from typing import Any

Handler = Callable[[argparse.Namespace], int]
Subparsers = "argparse._SubParsersAction[argparse.ArgumentParser]"

_REGISTERED: list[str] = []

GLOBAL_FLAGS_HELP = {
    "--json": "machine-readable output on stdout",
    "--dry-run": "do not write anything",
    "--data-dir": "override the data directory",
    "--yes": "confirm user-file changes already approved by the user",
}


def add_global_flags(parser: argparse.ArgumentParser, *, leaf: bool = False) -> None:
    """``--json`` / ``--dry-run`` / ``--data-dir`` / ``--yes``.

    On the top-level parser they are documented; on a leaf command they are accepted as well
    (``kallimachos archive run --dry-run`` works like ``kallimachos --dry-run archive run``) but
    hidden from that command's help and set only when given (``argparse.SUPPRESS`` default), so a
    leaf never overrides a value the user put before the command name.
    """
    hidden = argparse.SUPPRESS
    for name, help_text in GLOBAL_FLAGS_HELP.items():
        kwargs: dict[str, Any] = {"help": hidden if leaf else help_text}
        if name == "--data-dir":
            kwargs["metavar"] = "DIR"
            kwargs["default"] = hidden if leaf else None
        else:
            kwargs["action"] = "store_true"
            kwargs["default"] = hidden if leaf else False
        parser.add_argument(name, **kwargs)


def registered_commands() -> list[str]:
    """Every leaf command path registered so far (``"process note-write"``); for tests and docs."""
    return list(_REGISTERED)


_EID_RE = re.compile(r"^[0-9a-f]{12}$")


def eid_arg(value: str) -> str:
    """argparse ``type=`` for positional eids: 12 lower-case hex characters, nothing else (an eid
    becomes a file name under process/, so ``../x`` must never get that far)."""
    if not _EID_RE.match(value):
        raise argparse.ArgumentTypeError(f"not an eid (12 hex characters): {value!r}")
    return value


def add_command(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    handler: Handler,
    *,
    help: str,
    path: str,
    aliases: Sequence[str] = (),
) -> argparse.ArgumentParser:
    """Create a leaf sub-command bound to ``handler``. ``path`` is the full command path."""
    parser = sub.add_parser(name, help=help, description=help, aliases=list(aliases))
    parser.set_defaults(handler=handler, command_path=path)
    add_global_flags(parser, leaf=True)
    _REGISTERED.append(path)
    return parser


def register_leaf(parser: argparse.ArgumentParser, handler: Handler, *, path: str) -> None:
    """Bind a handler to a parser that was created directly (a group that is also a command)."""
    parser.set_defaults(handler=handler, command_path=path)
    add_global_flags(parser, leaf=True)
    _REGISTERED.append(path)


def add_group(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    *,
    help: str,
    path: str,
) -> argparse._SubParsersAction[argparse.ArgumentParser]:
    """Create a command group (``config``, ``process`` ...) and return its sub-parsers action."""
    parser = sub.add_parser(name, help=help, description=help)
    parser.set_defaults(command_path=path)
    return parser.add_subparsers(dest=f"{name.replace('-', '_')}_command", metavar="<command>")
