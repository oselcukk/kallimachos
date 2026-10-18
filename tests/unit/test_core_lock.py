"""state.lock: exclusive file locks, LockHeld, stale detection, clearing."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kallimachos.interfaces import ExitCode, LockHeld
from kallimachos.state.lock import FileLock, clear_stale_locks, stale_locks


def test_acquire_release_and_reacquire(tmp_path: Path) -> None:
    path = tmp_path / "locks" / "run.lock"
    with FileLock(path, owner="run daily") as lock:
        assert lock.held
        info = json.loads(path.read_text(encoding="utf-8"))
        assert info["owner"] == "run daily" and isinstance(info["pid"], int)
        assert not lock.is_stale()
    assert not path.exists()
    with FileLock(path):
        pass


def test_busy_lock_raises_lock_held(tmp_path: Path) -> None:
    path = tmp_path / "run.lock"
    first = FileLock(path, owner="first")
    first.acquire()
    try:
        second = FileLock(path, owner="second")
        with pytest.raises(LockHeld) as exc:
            second.acquire()
        assert exc.value.exit_code == ExitCode.LOCKED
        assert "first" in str(exc.value)
        with pytest.raises(LockHeld):
            second.acquire(timeout_s=0.1)
    finally:
        first.release()
    again = FileLock(path)
    again.acquire()
    again.release()


def test_lock_held_by_other_process(tmp_path: Path) -> None:
    path = tmp_path / "proc.lock"
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from kallimachos.state.lock import FileLock\n"
        "l = FileLock(Path(sys.argv[1]), owner='child'); l.acquire()\n"
        "print('ready', flush=True); time.sleep(30)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(path)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "ready"
        with pytest.raises(LockHeld):
            FileLock(path).acquire()
        assert not FileLock(path).is_stale()
    finally:
        child.kill()
        child.wait()
        if child.stdout is not None:
            child.stdout.close()
    # the OS released the lock with the process; the leftover file is stale and not blocking
    assert FileLock(path).is_stale()
    with FileLock(path):
        pass


def test_stale_detection_and_clearing(tmp_path: Path) -> None:
    locks = tmp_path / "locks"
    locks.mkdir()
    dead = locks / "dead.lock"
    dead.write_text(json.dumps({"pid": 999_999_999, "acquired_at": time.time()}), encoding="utf-8")
    old = locks / "old.lock"
    old.write_text(json.dumps({"pid": 1, "acquired_at": time.time() - 7 * 3600}), encoding="utf-8")
    leftover = locks / "leftover.lock"
    leftover.write_text("", encoding="utf-8")
    live = FileLock(locks / "live.lock", owner="live")
    live.acquire()
    try:
        assert FileLock(dead).is_stale()
        assert FileLock(old).is_stale()
        assert FileLock(leftover).is_stale()
        assert not FileLock(locks / "live.lock").is_stale()
        assert {p.name for p in stale_locks(locks)} == {"dead.lock", "old.lock", "leftover.lock"}
        removed = clear_stale_locks(locks)
        assert {p.name for p in removed} == {"dead.lock", "old.lock", "leftover.lock"}
        assert [p.name for p in locks.iterdir()] == ["live.lock"]
    finally:
        live.release()


def test_double_acquire_in_same_object_is_refused(tmp_path: Path) -> None:
    lock = FileLock(tmp_path / "x.lock")
    lock.acquire()
    with pytest.raises(LockHeld):
        lock.acquire()
    lock.release()
    lock.release()  # idempotent
