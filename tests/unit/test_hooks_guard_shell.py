"""PreToolUse guard against the bypasses found in an adversarial audit: a data directory with a
space (the macOS default), quoting and escapes, ``~`` / ``$HOME``, ``cd`` + relative paths,
symlinks, case variants, in-place editors, inline interpreter writes, ``apply_patch`` *Add File*
over a note and the ``> /dev/null`` false positive."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from kallimachos.hooks import guard
from kallimachos.paths import DataLayout

NOTE = """---
title: Example
---
# Example Author A (2021) - Fictional

intro

<!-- kallimachos:begin full_reading model=example-reader -->
## Full reading
BODY
<!-- kallimachos:end full_reading -->
tail
"""


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    data = home / "Library" / "Application Support" / "kallimachos"
    data.joinpath("state").mkdir(parents=True)
    data.joinpath("state", "seen.jsonl").write_text("{}\n", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(data, target_is_directory=True)
    note = tmp_path / "vault" / "Literature" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text(NOTE, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    return {"home": home, "data": data, "link": link, "note": note, "tmp": tmp_path}


def _bash(command: str, cwd: Path) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


def _decide(env: dict[str, Path], payload: dict[str, object]) -> str | None:
    return guard.decide(payload, DataLayout(env["data"]))


def test_state_file_written_through_quoting_and_expansion_is_denied(env: dict[str, Path]) -> None:
    d = env["data"]
    escaped = str(d).replace(" ", "\\ ")
    cases = [
        f'echo x > "{d}/state/seen.jsonl"',
        f"echo x > '{d}/state/seen.jsonl'",
        f"echo x > {escaped}/state/seen.jsonl",
        f'echo x >> "{d}/state/seen.jsonl"',
        f'cd "{d}" && echo x > state/seen.jsonl',
        f'cd "{d}/state"; rm seen.jsonl',
        f"echo x > {env['link']}/state/seen.jsonl",
        "echo x > ~/Library/Application\\ Support/kallimachos/state/seen.jsonl",
        'rm "$HOME/Library/Application Support/kallimachos/state/seen.jsonl"',
        'echo x | tee "$HOME/Library/Application Support/kallimachos/state/seen.jsonl"',
        f"python3 -c \"open('{env['link']}/state/seen.jsonl','w').write('1')\"",
        f"python3 -c \"from pathlib import Path; Path('{d}/state/seen.jsonl').write_text('x')\"",
        f'perl -pi -e s/a/b/ "{d}/state/seen.jsonl"',
        f"sed -i '' s/a/b/ '{d}/state/seen.jsonl'",
        f'cp /tmp/other "{d}/state/seen.jsonl"',
        f'mv "{d}/state/seen.jsonl" /tmp/x',
    ]
    for command in cases:
        reason = _decide(env, _bash(command, env["tmp"]))
        assert reason is not None and "seen.jsonl" in reason, command


def test_reads_and_dev_null_are_allowed(env: dict[str, Path]) -> None:
    d = env["data"]
    cases = [
        f'cat "{d}/state/seen.jsonl"',
        f'cat "{d}/state/seen.jsonl" > /dev/null',
        f'cat "{d}/state/seen.jsonl" | wc -l > /tmp/count.txt',
        f'grep foo "{d}/state/seen.jsonl" >> /tmp/hits.txt',
        f"python3 -c \"print(open('{d}/state/seen.jsonl').read())\"",
        f'cp "{d}/state/seen.jsonl" /tmp/backup.jsonl',
        f'kallimachos --data-dir "{d}" doctor',
        f'echo hi > "{d}/config.yaml"',
        "echo x > 'unbalanced",
    ]
    for command in cases:
        assert _decide(env, _bash(command, env["tmp"])) is None, command


def test_in_place_editors_on_a_managed_block_are_denied(env: dict[str, Path]) -> None:
    note = env["note"]
    for command in (
        f"perl -pi -e s/BODY/HACK/ {note}",
        f"sed -i '' s/BODY/HACK/ {note}",
        f"sed --in-place s/BODY/HACK/ '{note}'",
        f"echo new > {note}",
        f"rm {note}",
        f"python3 -c \"open('{note}','w').write('x')\"",
    ):
        reason = _decide(env, _bash(command, env["tmp"]))
        assert reason is not None and "full_reading" in reason, command
    for command in (f"cat {note}", f"grep Theorem {note} > /tmp/out", f"sed s/BODY/HACK/ {note}"):
        assert _decide(env, _bash(command, env["tmp"])) is None, command


def test_apply_patch_add_file_over_a_note_is_denied(env: dict[str, Path]) -> None:
    note = env["note"]
    patch = f"*** Begin Patch\n*** Add File: {note}\n+hello\n*** End Patch"
    payload = {"tool_name": "apply_patch", "tool_input": {"command": patch}, "cwd": str(env["tmp"])}
    reason = _decide(env, payload)
    assert reason is not None and "full_reading" in reason
    fresh = f"*** Begin Patch\n*** Add File: {env['tmp']}/new.md\n+hello\n*** End Patch"
    payload = {"tool_name": "apply_patch", "tool_input": {"command": fresh}, "cwd": str(env["tmp"])}
    assert _decide(env, payload) is None


@pytest.mark.skipif(sys.platform not in ("darwin", "win32"), reason="case-insensitive default fs")
def test_case_variant_path_is_denied_on_case_insensitive_systems(env: dict[str, Path]) -> None:
    upper = str(env["data"]).replace("kallimachos", "KALLIMACHOS") + "/state/seen.jsonl"
    payload = {"tool_name": "Write", "tool_input": {"file_path": upper, "content": "x"}}
    assert _decide(env, payload) is not None


def test_write_candidates_follow_cd_and_expand(env: dict[str, Path]) -> None:
    cwd = str(env["tmp"])
    found = guard.write_candidates('cd "$HOME/Library" && echo x > "Application Support/k/s"', cwd)
    assert found == [os.path.join(str(env["home"]), "Library", "Application Support", "k", "s")]
    assert guard.write_candidates("cat a b | sort", cwd) == []
    assert guard.write_candidates("cp a b", cwd) == [os.path.join(cwd, "b")]
