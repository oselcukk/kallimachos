"""``.claude/skills/`` is an exact copy of ``.agents/skills/`` (tools/sync_skills.py)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _tool(repo_root: Path) -> ModuleType:
    path = repo_root / "tools" / "sync_skills.py"
    spec = importlib.util.spec_from_file_location("sync_skills", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_claude_skills_mirror_is_current(repo_root: Path) -> None:
    diff = _tool(repo_root).differences()
    assert not diff, "run `python tools/sync_skills.py`: " + "; ".join(diff)


def test_every_skill_has_frontmatter(repo_root: Path) -> None:
    skills = sorted((repo_root / ".agents" / "skills").glob("*/SKILL.md"))
    assert {p.parent.name for p in skills} >= {"install", "run", "feedback", "process", "doctor"}
    for path in skills:
        head = path.read_text(encoding="utf-8").split("---")[1]
        assert "name: kallimachos-" in head and "description:" in head, path


def test_sync_copies_and_removes(tmp_path: Path, repo_root: Path) -> None:
    tool = _tool(repo_root)
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "a").mkdir(parents=True)
    (src / "a" / "SKILL.md").write_text("one", encoding="utf-8")
    (dst / "old").mkdir(parents=True)
    (dst / "old" / "SKILL.md").write_text("stale", encoding="utf-8")
    (dst / "README.md").write_text("kept", encoding="utf-8")
    assert tool.differences(src, dst)
    tool.sync(src, dst)
    assert tool.differences(src, dst) == []
    assert (dst / "README.md").read_text(encoding="utf-8") == "kept"
    assert not (dst / "old").exists()
    (src / "a" / "SKILL.md").write_text("two", encoding="utf-8")
    assert tool.differences(src, dst) == ["differs a/SKILL.md"]


@pytest.mark.parametrize("name", ["INSTALL.md", "INSTALL.tr.md"])
def test_install_entry_points_cover_both_systems(repo_root: Path, name: str) -> None:
    text = (repo_root / name).read_text(encoding="utf-8")
    for needle in (
        "github.com/oselcukk/kallimachos",
        "python3 -m venv .venv",
        "py -3 -m venv .venv",
        "pip install -e .",
        "kallimachos doctor",
        ".agents/skills/install/SKILL.md",
        "kallimachos profile approve",
        "kallimachos notify confirm-email",
        "hooks install",
        "schedule install",
        "kallimachos run daily --first-run",
        "archive/refs/heads/main.zip",
    ):
        assert needle in text, (name, needle)
