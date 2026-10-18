"""Repository hygiene checks.

English-only code and identifiers; no personal data; no token-like strings.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TURKISH = re.compile("[\u00e7\u011f\u0131\u00f6\u015f\u00fc\u00c7\u011e\u0130\u00d6\u015e\u00dc]")

# Files that legitimately contain Turkish characters.
ALLOWED = {
    "README.tr.md",
    "INSTALL.tr.md",
    "LICENSE",  # copyright holder name
    "pyproject.toml",  # author name; dotless-i in allowed-confusables
    "config.example.yaml",  # default sensitive-path regex contains Turkish words
    "src/kallimachos/i18n/tr.json",
    "src/kallimachos/config/schema.py",  # default sensitive-path regex (data)
    "src/kallimachos/records/normalize.py",  # dotless-i handling in match_key
    "src/kallimachos/process/status.py",  # receipt label the Avenox beyin skill requires verbatim
}
ALLOWED_PREFIXES = (".agents/skills/", ".claude/skills/", "tests/")
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache"}


def _files() -> list[Path]:
    out: list[Path] = []
    for path in REPO.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts) or not path.is_file():
            continue
        if path.suffix in {".png", ".pdf", ".jpg", ".ico"}:
            continue
        out.append(path)
    return out


def test_turkish_characters_only_in_allowed_files() -> None:
    offenders: list[str] = []
    for path in _files():
        rel = path.relative_to(REPO).as_posix()
        if rel in ALLOWED or rel.startswith(ALLOWED_PREFIXES):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if TURKISH.search(text):
            offenders.append(rel)
    assert not offenders, offenders


def test_no_token_like_secrets_in_tree() -> None:
    token = re.compile(
        r"(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|bot[0-9]{6,}:[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16})"
    )
    offenders = [
        p.relative_to(REPO).as_posix()
        for p in _files()
        if "tests/fixtures" not in p.as_posix()
        and token.search(p.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert not offenders, offenders
