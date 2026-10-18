"""paths: data directory resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from kallimachos.paths import DataLayout, data_dir, default_data_dir


def test_env_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KALLIMACHOS_HOME", str(tmp_path / "custom"))
    assert data_dir() == (tmp_path / "custom").resolve()


def test_explicit_override_beats_env(tmp_path: Path) -> None:
    assert data_dir(tmp_path / "cli") == (tmp_path / "cli").resolve()


def test_os_defaults() -> None:
    env = {"HOME": "/home/u"}
    assert default_data_dir("darwin", env) == Path(
        "/home/u/Library/Application Support/kallimachos"
    )
    assert default_data_dir("linux", env) == Path("/home/u/.local/share/kallimachos")
    assert default_data_dir("linux", {**env, "XDG_DATA_HOME": "/data"}) == Path("/data/kallimachos")
    assert default_data_dir("win32", {**env, "LOCALAPPDATA": "C:/Users/u/AppData/Local"}) == Path(
        "C:/Users/u/AppData/Local/kallimachos"
    )
    assert default_data_dir("win32", env) == Path("/home/u/AppData/Local/kallimachos")


def test_layout_paths(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path)
    assert layout.config_file == tmp_path / "config.yaml"
    assert layout.seen_file == tmp_path / "state" / "seen.jsonl"
    assert layout.run_file("2026-10-18-daily") == tmp_path / "runs" / "2026-10-18-daily.json"
    assert layout.selection_file("x") == tmp_path / "runs" / "x.selection.json"
    assert layout.lock_file("openalex") == tmp_path / "state" / "locks" / "openalex.lock"
    assert layout.notes_index_file == tmp_path / "process" / "notes_index.json"
    assert all(p.is_relative_to(tmp_path) for p in layout.required_dirs())
