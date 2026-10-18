"""File primitives of the bridge: sha256, cloud placeholders, non-overwriting copy and
move, path safety and marked (program-owned) text files.

Permanent rules enforced here: nothing is ever overwritten (``O_EXCL`` / hard links), the only
file the bridge may remove is a partial copy it created itself in the same call, and paths that
leave the target root or pass through a symlink are rejected.
"""

from __future__ import annotations

import errno
import os
import re
from pathlib import Path

from kallimachos.archive.files import SF_DATALESS, is_dataless, is_regular_file, sha256_file
from kallimachos.state import store

__all__ = [
    "INCOMING_DIR_NAME",
    "MARKER",
    "SF_DATALESS",
    "copy_exclusive",
    "copy_verified",
    "inside_root",
    "is_dataless",
    "is_regular_file",
    "move_exclusive",
    "safe_folder",
    "sha256_file",
    "write_marked",
]

#: One folder component: word characters first, then word characters, space, dot, dash.
_FOLDER_PART_RE = re.compile(r"^\w[\w .-]{0,120}$")

#: Errors that mean "hard links are not possible here, fall back to a verified copy".
_NO_LINK_ERRNOS = {errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.ENOSYS, errno.EMLINK}

INCOMING_DIR_NAME = "_Incoming"


def safe_folder(folder: str) -> bool:
    """A catalog folder: relative, POSIX separators, no ``..``/hidden/empty parts, not the
    ``_Incoming`` drop folder. ``""`` (archive root) is allowed."""
    if folder == "":
        return True
    if folder.startswith("/") or "\\" in folder:
        return False
    parts = folder.split("/")
    if parts[0] == INCOMING_DIR_NAME:
        return False
    return all(p not in ("", ".", "..") and _FOLDER_PART_RE.match(p) for p in parts)


def inside_root(path: Path, root: Path) -> bool:
    """``path`` resolves inside ``root`` and no existing component between them is a symlink."""
    root_real = Path(os.path.realpath(root))
    target_real = Path(os.path.realpath(path))
    if target_real == root_real or root_real not in target_real.parents:
        return False
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


def copy_exclusive(source: Path, target: Path) -> None:
    """Copy with ``O_CREAT | O_EXCL``: raises :class:`FileExistsError` when ``target`` exists.

    On a failure while writing, only the file this call created is removed."""
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as src:
            for block in iter(lambda: src.read(1 << 20), b""):
                out.write(block)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    st = source.stat()
    os.utime(target, (st.st_atime, st.st_mtime))


def copy_verified(source: Path, target: Path, sha256: str) -> None:
    """``O_EXCL`` copy followed by a sha256 check of the written file. On mismatch the copy (which
    this call created) is removed and :class:`OSError` is raised."""
    copy_exclusive(source, target)
    if sha256_file(target) != sha256:
        target.unlink(missing_ok=True)
        raise OSError(errno.EIO, "sha256 of the written copy does not match", str(target))


def move_exclusive(source: Path, target: Path, sha256: str) -> None:
    """Move without overwriting: hard link, then drop the source name. Across file systems: a
    verified ``O_EXCL`` copy, then the source name is dropped. :class:`FileExistsError` when
    ``target`` exists (the source stays where it is)."""
    try:
        os.link(source, target)
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in _NO_LINK_ERRNOS:
            raise
        copy_verified(source, target, sha256)
    source.unlink()


MARKER = "<!-- kallimachos:bridge -->"


def write_marked(path: Path, text: str) -> bool:
    """Write a program-owned Markdown file. An existing file is replaced only when it carries
    :data:`MARKER` (an unmarked file was written or edited by the user and is left alone) and is
    not a cloud placeholder. Returns whether the file now holds ``text``."""
    body = text if text.startswith(MARKER) else f"{MARKER}\n{text}"
    if path.exists():
        if path.is_symlink() or is_dataless(path):
            return False
        old = path.read_text(encoding="utf-8", errors="replace")
        if MARKER not in old:
            return False
        if old == body:
            return True
    store.write_text_atomic(path, body)
    return True
