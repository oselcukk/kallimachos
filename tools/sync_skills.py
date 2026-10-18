"""Mirror ``.agents/skills/`` into ``.claude/skills/``.

The skills are written once in ``.agents/skills/`` (read by Codex, Hermes and other agents).
Claude Code discovers project skills in ``.claude/skills/<name>/SKILL.md``. A plain copy works on
every OS (symlinks need extra settings on Windows), so the mirror is a copy and
``tests/e2e/test_skill_mirror.py`` fails when it is out of date.

    python tools/sync_skills.py          # refresh the copy
    python tools/sync_skills.py --check  # exit 1 when the copy differs
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / ".agents" / "skills"
MIRROR = REPO / ".claude" / "skills"
KEEP = {"README.md"}  # explains the mirror; not a skill


def differences(source: Path = SOURCE, mirror: Path = MIRROR) -> list[str]:
    """Relative paths that are missing, extra or different in the mirror."""
    out: list[str] = []
    src_files = {p.relative_to(source) for p in source.rglob("*") if p.is_file()}
    dst_files = {
        p.relative_to(mirror)
        for p in mirror.rglob("*")
        if p.is_file() and p.relative_to(mirror).as_posix() not in KEEP
    }
    for rel in sorted(src_files - dst_files):
        out.append(f"missing {rel.as_posix()}")
    for rel in sorted(dst_files - src_files):
        out.append(f"extra {rel.as_posix()}")
    for rel in sorted(src_files & dst_files):
        if not filecmp.cmp(source / rel, mirror / rel, shallow=False):
            out.append(f"differs {rel.as_posix()}")
    return out


def sync(source: Path = SOURCE, mirror: Path = MIRROR) -> None:
    mirror.mkdir(parents=True, exist_ok=True)
    for child in mirror.iterdir():
        if child.name in KEEP:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    for skill in sorted(p for p in source.iterdir() if p.is_dir()):
        shutil.copytree(skill, mirror / skill.name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only report differences")
    args = parser.parse_args(argv)
    if args.check:
        diff = differences()
        for line in diff:
            print(line)
        return 1 if diff else 0
    sync()
    print(f"mirrored {SOURCE.relative_to(REPO)} -> {MIRROR.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
