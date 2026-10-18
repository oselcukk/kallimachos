"""CLI skeleton: every documented command is registered.

A body that is still a stub reports "not implemented" with exit code 1; only the commands listed
in ``STUB_COMMANDS`` may do so (the set is empty: every command is implemented).
"""

from __future__ import annotations

import argparse
import io
from collections.abc import Iterator
from pathlib import Path

import pytest

from kallimachos.cli import build_parser, main
from kallimachos.cli_support import registered_commands
from kallimachos.interfaces import ExitCode
from kallimachos.version import __version__

SPEC_COMMANDS = {
    "init",
    "config validate",
    "config show",
    "config migrate",
    "config docs",
    "profile build",
    "profile show",
    "profile approve",
    "sources list",
    "sources test",
    "run daily",
    "run weekly",
    "select",
    "count",
    "feedback add",
    "feedback parse",
    "feedback poll",
    "feedback apply-weights",
    "archive run",
    "archive retry",
    "archive status",
    "archive scan-existing",
    "archive confirm",
    "process chunk create",
    "process extract",
    "process quote",
    "process card-write",
    "process check",
    "process cite",
    "process semantic-record",
    "process status",
    "process migrate-card",
    "process note-write",
    "process links",
    "process links fix-fake",
    "process cluster start",
    "process cluster write",
    "process cluster open-problems",
    "process queue add",
    "process queue sync",
    "process notes-index rebuild",
    "process notes-index find",
    "process zotero ris",
    "process zotero fix-metadata",
    "process zotero split-names",
    "process zotero reset-citekeys",
    "process zotero link",
    "bridge push",
    "bridge pull",
    "bridge deliver",
    "bridge status",
    "bridge push-ack",
    "bridge reconcile",
    "notify test",
    "notify confirm-email",
    "model test",
    "schedule install",
    "schedule remove",
    "schedule status",
    "schedule run-missed",
    "hooks install",
    "hooks uninstall",
    "hooks status",
    "hooks session-start",
    "hooks guard",
    "doctor",
}


STUB_COMMANDS: set[str] = set()
"""Commands that may still raise ``NotImplementedError``: none, every command is implemented."""


def _leaf_parsers(
    parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], argparse.ArgumentParser]]:
    subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if parser.get_default("handler") is not None:
        yield prefix, parser
    for sub in subs:
        for name, child in sub.choices.items():
            yield from _leaf_parsers(child, (*prefix, name))


def _minimal_args(parser: argparse.ArgumentParser) -> list[str]:
    """Supply a dummy value for every required argument so the handler is reached."""
    args: list[str] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction | argparse._HelpAction):
            continue
        value = str(action.choices[0]) if action.choices else ("1" if action.type is int else "x")  # type: ignore[index]
        if not action.option_strings:
            if action.nargs in (None, 1, "+"):
                args.append(value)
        elif action.required:
            args.extend([action.option_strings[0], value])
    return args


def test_every_spec_command_is_registered() -> None:
    build_parser()
    assert set(registered_commands()) >= SPEC_COMMANDS, SPEC_COMMANDS - set(registered_commands())


def test_help_lists_top_level_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for word in (
        "init",
        "config",
        "profile",
        "sources",
        "run",
        "select",
        "count",
        "feedback",
        "archive",
        "process",
        "bridge",
        "notify",
        "model",
        "schedule",
        "doctor",
        "Exit codes",
    ):
        assert word in out


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == ExitCode.USAGE
    assert "usage:" in capsys.readouterr().err


def test_bare_group_prints_group_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["process"])
    assert exc.value.code == 0
    assert "note-write" in capsys.readouterr().out


def test_unknown_command_is_usage_error() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["frobnicate"])
    assert exc.value.code == 2


@pytest.mark.parametrize(
    ("path", "parser"),
    list(_leaf_parsers(build_parser())),
    ids=lambda v: " ".join(v) if isinstance(v, tuple) else "",
)
def test_only_listed_stubs_report_not_implemented(
    path: tuple[str, ...],
    parser: argparse.ArgumentParser,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Every handler is reached with dummy arguments against an empty data directory. Implemented
    commands may fail with any exit code (missing files, usage errors) but never with the
    "not implemented" message; stubs listed in ``STUB_COMMANDS`` must still report it."""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setenv("KALLIMACHOS_HOME", str(tmp_path))
    monkeypatch.setenv("OFFLINE", "1")
    monkeypatch.chdir(tmp_path)
    try:
        code = main(["--data-dir", str(tmp_path), *path, *_minimal_args(parser)])
    except SystemExit as exc:  # argparse rejected a dummy value: the handler was not reached
        assert exc.code == 2
        return
    except Exception as exc:  # an implemented body may trip on dummy input
        assert " ".join(path) not in STUB_COMMANDS, exc
        return
    err = capsys.readouterr().err
    stub = "not implemented yet" in err
    if " ".join(path) in STUB_COMMANDS:
        assert stub and code == ExitCode.UNEXPECTED
        assert " ".join(path) in err
    else:
        assert not stub, err


def test_every_leaf_has_help(capsys: pytest.CaptureFixture[str]) -> None:
    for path, _ in _leaf_parsers(build_parser()):
        with pytest.raises(SystemExit) as exc:
            main([*path, "--help"])
        assert exc.value.code == 0, path
        capsys.readouterr()


def test_schedule_uninstall_is_an_alias_of_remove() -> None:
    parser = build_parser()
    remove = parser.parse_args(["schedule", "remove"])
    uninstall = parser.parse_args(["schedule", "uninstall"])
    assert uninstall.handler is remove.handler and uninstall.command_path == "schedule remove"
    with pytest.raises(SystemExit) as exc:
        main(["schedule", "uninstall", "--help"])
    assert exc.value.code == 0


def test_global_flags_are_accepted_before_the_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--json", "--dry-run", "--yes", "--data-dir", str(tmp_path), "doctor"])
    assert code in (ExitCode.OK, ExitCode.UNEXPECTED)
    assert "not implemented" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["--json", "--dry-run", "--yes", "doctor"],
        ["doctor", "--json", "--dry-run", "--yes"],
        ["archive", "run", "--dry-run", "--json"],
        ["feedback", "apply-weights", "--dry-run"],
        ["process", "links", "--dry-run"],
    ],
)
def test_global_flags_are_accepted_after_the_subcommand(argv: list[str]) -> None:
    args = build_parser().parse_args(argv)
    assert args.dry_run is True
    if "--json" in argv:
        assert args.json is True
    if "--yes" in argv:
        assert args.yes is True


def test_leaf_flags_do_not_reset_values_given_before_the_command(tmp_path: Path) -> None:
    args = build_parser().parse_args(["--json", "--data-dir", str(tmp_path), "doctor"])
    assert args.json is True and args.data_dir == str(tmp_path) and args.dry_run is False
    args = build_parser().parse_args(["doctor", "--data-dir", str(tmp_path)])
    assert args.data_dir == str(tmp_path) and args.json is False


def test_leaf_help_does_not_repeat_global_flags(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["archive", "run", "--help"])
    out = capsys.readouterr().out
    assert "--dry-run" not in out  # documented once, on `kallimachos --help`
