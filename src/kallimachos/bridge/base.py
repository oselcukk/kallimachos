"""Transport abstraction for the bridge.

The computer always connects to the server (pull direction). A transport knows the server's
Kallimachos data directory (``remote_data_dir``); every ``remote_relative`` path is relative to it.

* ``pull``: copy a remote file, or a remote directory tree (``remote_relative`` ending in ``/``),
  into a local cache. Only regular files are copied; symlinks and special files are skipped.
* ``push``: copy one local file to a *new* remote path; an existing remote file is never replaced.
* ``run``: run a command on the server and return ``(returncode, stdout, stderr)``.

:class:`LocalTransport` is the ``file://`` driver used by the tests: a local folder plays the
server's data directory and ``kallimachos`` commands run in-process against it.
"""

from __future__ import annotations

import contextlib
import fnmatch
import io
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from kallimachos.bridge.files import copy_exclusive, is_regular_file
from kallimachos.interfaces import ExitCode, KallimachosError

Runner = Callable[[list[str]], tuple[int, str, str]]


class TransportError(KallimachosError):
    """The server could not be reached or a transfer failed (exit 6)."""

    exit_code = ExitCode.EXHAUSTED


class BridgeConflict(KallimachosError):
    """CONFLICT: target exists with a different sha; or CATALOG_DIVERGED; or the two catalog
    copies disagree on a work's sha256 (exit 3: the round stopped, nothing was overwritten)."""

    exit_code = ExitCode.PARTIAL


class Transport(Protocol):
    remote_data_dir: str

    def pull(
        self,
        remote_relative: str,
        local: Path,
        *,
        pattern: str | None = None,
        missing_ok: bool = False,
    ) -> None: ...

    def push(self, local: Path, remote_relative: str) -> None: ...

    def run(self, argv: list[str]) -> tuple[int, str, str]: ...

    def close(self) -> None: ...


def kallimachos_argv(transport: Transport, *args: str) -> list[str]:
    """``kallimachos --data-dir <remote data dir> <args>`` for :meth:`Transport.run`."""
    return ["kallimachos", "--data-dir", transport.remote_data_dir, *args]


def _replace_from(source: Path, target: Path) -> None:
    """Refresh a cache file: copy beside it, then ``os.replace`` (cache only, not the archive)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".pull-")
    os.close(fd)
    try:
        shutil.copy2(source, tmp)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class LocalTransport:
    """``file://`` transport for tests: a local folder plays the remote data dir."""

    def __init__(self, remote_root: Path, *, runner: Runner | None = None) -> None:
        self.remote_root = remote_root
        self.remote_data_dir = str(remote_root)
        self._runner = runner

    def _remote(self, remote_relative: str) -> Path:
        rel = Path(remote_relative)
        if rel.is_absolute() or ".." in rel.parts:
            raise TransportError(f"remote path must be relative: {remote_relative!r}")
        return self.remote_root / rel

    def pull(
        self,
        remote_relative: str,
        local: Path,
        *,
        pattern: str | None = None,
        missing_ok: bool = False,
    ) -> None:
        source = self._remote(remote_relative)
        if remote_relative.endswith("/"):
            if not source.is_dir() or source.is_symlink():
                if missing_ok:
                    return
                raise TransportError(f"remote directory missing: {remote_relative}")
            for dirpath, dirnames, filenames in os.walk(source):
                dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
                for name in sorted(filenames):
                    path = Path(dirpath) / name
                    if not is_regular_file(path):
                        continue
                    if pattern and not fnmatch.fnmatch(name, pattern):
                        continue
                    _replace_from(path, local / path.relative_to(source))
            return
        if not is_regular_file(source):
            if missing_ok:
                return
            raise TransportError(f"remote file missing: {remote_relative}")
        _replace_from(source, local)

    def push(self, local: Path, remote_relative: str) -> None:
        target = self._remote(remote_relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            copy_exclusive(local, target)
        except FileExistsError:
            raise TransportError(f"remote file exists, not replaced: {remote_relative}") from None

    def run(self, argv: list[str]) -> tuple[int, str, str]:
        if self._runner is not None:
            return self._runner(argv)
        if argv and argv[0] == "kallimachos":
            from kallimachos.cli import main  # local import: cli imports this package

            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(argv[1:])
            return code, out.getvalue(), err.getvalue()
        proc = subprocess.run(
            argv, cwd=self.remote_root, capture_output=True, text=True, check=False
        )
        return proc.returncode, proc.stdout, proc.stderr

    def close(self) -> None:
        return None
