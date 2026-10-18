"""Cross-platform file locks under ``state/locks/<name>.lock``.

``flock`` on POSIX, ``msvcrt.locking`` on Windows. The lock file records owner PID and
acquisition time so ``doctor --fix`` can clear stale locks.

The OS lock is the source of truth: a crashed process releases it automatically, so a leftover
file never blocks a new run. A file whose owner is gone, or that is older than
``stale_after_s``, is *stale*; ``doctor --fix`` deletes it (:func:`clear_stale_locks`).
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import time
from pathlib import Path
from types import TracebackType
from typing import IO, Any

from kallimachos.interfaces import LockHeld

if sys.platform == "win32":  # pragma: no cover - exercised on the Windows CI runner
    import msvcrt

    # Windows byte-range locks are mandatory: locking byte 0 would make the owner record
    # unreadable for everyone else. Lock one byte far past any content instead (allowed beyond
    # end of file), so ``read_lock_info`` and ``doctor`` can still read who holds it.
    _LOCK_OFFSET = 1 << 30

    def _try_lock(fh: IO[str]) -> bool:
        try:
            fh.seek(_LOCK_OFFSET)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fh: IO[str]) -> None:
        with contextlib.suppress(OSError):
            fh.seek(_LOCK_OFFSET)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fh: IO[str]) -> bool:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fh: IO[str]) -> None:
        with contextlib.suppress(OSError):
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def pid_alive(pid: int) -> bool:
    """Whether a process with ``pid`` exists (best effort, never raises)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":  # pragma: no cover - Windows only
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, owned by someone else
        except OSError:
            return False
        return True


def read_lock_info(path: Path) -> dict[str, Any] | None:
    """Owner record of a lock file (``pid``, ``owner``, ``acquired_at``, ``host``) or ``None``."""
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return None
    try:
        data = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class FileLock:
    """Context manager. Raises :class:`~kallimachos.interfaces.LockHeld` (exit 4) when busy."""

    def __init__(self, path: Path, *, owner: str = "", stale_after_s: float = 6 * 3600) -> None:
        self.path = path
        self.owner = owner
        self.stale_after_s = stale_after_s
        self._fh: IO[str] | None = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def _attempt(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115 - kept open while held
        if not _try_lock(fh):
            fh.close()
            return False
        # The file may have been unlinked by a releasing holder between our open and lock:
        # only a lock on the inode that is still at ``path`` counts.
        try:
            same = os.path.samestat(os.fstat(fh.fileno()), os.stat(self.path))
        except FileNotFoundError:
            same = False
        if not same:
            _unlock(fh)
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        info = {
            "pid": os.getpid(),
            "owner": self.owner,
            "acquired_at": time.time(),
            "host": socket.gethostname() if hasattr(socket, "gethostname") else "",
        }
        fh.write(json.dumps(info))
        fh.flush()
        self._fh = fh
        return True

    def acquire(self, *, timeout_s: float = 0.0) -> None:
        """Acquire or raise ``LockHeld``."""
        if self._fh is not None:
            raise LockHeld(f"lock {self.path.name} is already held by this process")
        deadline = time.monotonic() + max(timeout_s, 0.0)
        while True:
            if self._attempt():
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        info = read_lock_info(self.path) or {}
        detail = f"pid {info.get('pid', '?')}"
        if info.get("owner"):
            detail += f" ({info['owner']})"
        hint = "; it looks stale, run `kallimachos doctor --fix`" if self.is_stale() else ""
        raise LockHeld(f"lock {self.path.name} is held by {detail}{hint}")

    def release(self) -> None:
        fh = self._fh
        if fh is None:
            return
        self._fh = None
        if sys.platform == "win32":
            # an open file cannot be deleted on Windows: close first, then delete; the delete
            # fails harmlessly while another process has the file open (it may be locking it)
            _unlock(fh)
            fh.close()
            with contextlib.suppress(OSError):
                self.path.unlink()
        else:
            # unlink while still holding the lock, so nobody can lock the old inode afterwards
            with contextlib.suppress(OSError):
                self.path.unlink()
            _unlock(fh)
            fh.close()

    def is_stale(self, *, now: float | None = None) -> bool:
        """True when the lock file exists, its owner PID is gone or it is older than
        ``stale_after_s``.

        A file left behind by a crash (not locked by anyone) is stale as well.
        """
        if not self.path.exists():
            return False
        info = read_lock_info(self.path)
        if info is None:
            return not self._locked_by_other()
        pid = info.get("pid")
        if isinstance(pid, int) and pid != os.getpid() and not pid_alive(pid):
            return True
        acquired = info.get("acquired_at")
        moment = time.time() if now is None else now
        if isinstance(acquired, int | float) and moment - acquired > self.stale_after_s:
            return True
        return not self._locked_by_other()

    def _locked_by_other(self) -> bool:
        if self._fh is not None:
            return True
        try:
            fh = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115
        except OSError:
            return True
        try:
            if _try_lock(fh):
                _unlock(fh)
                return False
            return True
        finally:
            fh.close()

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def stale_locks(lock_dir: Path, *, stale_after_s: float = 6 * 3600) -> list[Path]:
    """Lock files under ``lock_dir`` that are stale."""
    if not lock_dir.is_dir():
        return []
    return [
        p
        for p in sorted(lock_dir.glob("*.lock"))
        if FileLock(p, stale_after_s=stale_after_s).is_stale()
    ]


def clear_stale_locks(lock_dir: Path, *, stale_after_s: float = 6 * 3600) -> list[Path]:
    """Delete stale lock files (``doctor --fix``); return the removed paths."""
    removed: list[Path] = []
    for p in stale_locks(lock_dir, stale_after_s=stale_after_s):
        with contextlib.suppress(FileNotFoundError):
            p.unlink()
            removed.append(p)
    return removed
