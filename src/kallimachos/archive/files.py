"""File primitives shared by the archive and the bridge: sha256, macOS cloud
placeholders, regular-file check. One implementation for both sides of the bridge."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path

#: macOS ``SF_DATALESS`` (<sys/stat.h>): the file is a cloud placeholder whose bytes are not on
#: disk (iCloud / Drive "optimise storage"). Other platforms have no portable flag.
SF_DATALESS = 0x40000000


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def is_dataless(path: Path) -> bool:
    """True for a macOS cloud placeholder; always False on other platforms (v1: macOS only)."""
    if sys.platform != "darwin":
        return False
    try:
        flags = getattr(os.lstat(path), "st_flags", 0)
    except OSError:
        return False
    return bool(flags & SF_DATALESS)


def is_regular_file(path: Path) -> bool:
    """Regular file that is not a symlink."""
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode)
