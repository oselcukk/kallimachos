"""Avenox integration: official avenoxbeyin release in a temporary vault.

``tools/fetch_avenox.py`` downloads and verifies the release ZIP (network), the official
installer sets up a fresh vault with a temporary HOME, then a synthetic work goes through
``process note-write`` (which runs ``beyin.py sync``) and ``beyin.py context`` must find the note
by its title.
Skipped unless ``KALLIMACHOS_E2E_NETWORK=1``; skipped as well when GitHub cannot be reached.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import urllib.error
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from kallimachos.cli import main
from kallimachos.interfaces import WorkState, dump
from kallimachos.paths import DataLayout
from tests.unit.test_process_notes_support import (
    EID_A,
    body_md,
    card,
    catalog_entry,
    chain_to,
    make_config,
    set_state,
    write_card,
    write_catalog,
    write_semantic,
)

pytestmark = pytest.mark.e2e_network


def _fetch_tool(repo_root: Path) -> ModuleType:
    path = repo_root / "tools" / "fetch_avenox.py"
    spec = importlib.util.spec_from_file_location("fetch_avenox", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _beyin(vault: Path, env: dict[str, str], *args: str) -> dict[str, Any]:
    res = subprocess.run(
        [sys.executable, "-I", str(vault / "beyin.py"), "--json", *args],
        cwd=vault,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert res.returncode == 0, res.stderr[-2000:]
    data: dict[str, Any] = json.loads(res.stdout)
    return data


def test_note_write_reaches_avenox(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _isolated_home: Path
) -> None:
    tool = _fetch_tool(repo_root)
    try:
        release = tool.fetch(dest=tmp_path / "cache")
    except (urllib.error.URLError, OSError) as exc:
        pytest.skip(f"GitHub not reachable: {exc}")

    home = tmp_path / "home"
    home.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    env.pop("KALLIMACHOS_HOME", None)
    monkeypatch.setenv("HOME", str(home))
    install = subprocess.run(
        [
            sys.executable,
            "-I",
            release["installer"],
            "--vault",
            str(vault),
            "--state",
            str(tmp_path / "beyin-state"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert install.returncode == 0, install.stderr[-2000:]
    assert (vault / "beyin.py").is_file() and (vault / ".beyin-runtime.json").is_file()
    assert not any(home.iterdir()), "the installer must not write into HOME"

    layout = DataLayout(_isolated_home)
    for d in layout.required_dirs():
        d.mkdir(parents=True, exist_ok=True)
    config = make_config(vault)
    layout.config_file.write_text(yaml.safe_dump(dump(config), sort_keys=False), "utf-8")
    assert main(["config", "validate"]) == 0
    write_catalog(layout, catalog_entry())
    write_card(layout, card())
    set_state(layout, EID_A, *chain_to(WorkState.SEMANTIC_VERIFIED))
    write_semantic(layout, EID_A)
    body = tmp_path / "note.md"
    body.write_text(body_md(), encoding="utf-8")

    capsys_out = tmp_path / "out.json"
    with capsys_out.open("w", encoding="utf-8") as fh:
        monkeypatch.setattr("sys.stdout", fh)
        code = main(["--json", "process", "note-write", EID_A, "--file", str(body)])
    assert code == 0
    out = json.loads(capsys_out.read_text(encoding="utf-8"))
    note = Path(out["path"])
    assert note.is_file() and vault in note.parents
    assert out["sync"] == "succeeded", out
    assert f"eid: {EID_A}" in note.read_text(encoding="utf-8")

    sync = _beyin(vault, env, "sync")
    assert sync.get("conflicts") in ([], None), sync
    # Avenox retrieval is text-based: a bare eid query abstains (observed with 3.11.0), the
    # title words find the note and the record carries the Kallimachos frontmatter (eid).
    found = _beyin(vault, env, "context", "Fictional Zeta Sums", "--no-sync")
    records = found.get("records", [])
    assert any(r.get("eid") == EID_A and r.get("kallimachos") == "note/1" for r in records), found

    # reading queue -> Avenox task (task-create), not created twice
    with capsys_out.open("w", encoding="utf-8") as fh:
        monkeypatch.setattr("sys.stdout", fh)
        assert main(["process", "queue", "add", EID_A, "--kind", "read"]) == 0
        assert main(["process", "queue", "sync"]) == 0
        assert main(["process", "queue", "sync"]) == 0
    tasks = [p for p in (vault / "tasks").rglob("*.md")] if (vault / "tasks").is_dir() else []
    matching = [p for p in tasks if "Fictional Zeta" in p.read_text(encoding="utf-8")]
    assert len(matching) == 1, [p.name for p in tasks]
