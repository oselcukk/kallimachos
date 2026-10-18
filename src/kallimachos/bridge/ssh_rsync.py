"""ssh + rsync transport, key auth only, single master connection, ``bridge.retry``.

* One SSH master connection per bridge round (``ControlMaster=yes``, ``ControlPersist=no``);
  every ``rsync``/``ssh`` call reuses it through ``ControlPath``. ``BatchMode=yes`` is always set,
  so a password prompt can never appear.
* Opening the master is retried ``retry`` times (default 1: fail2ban friendly); transfers are not
  retried on their own because they ride the already-open master.
* Remote paths are quoted for the remote shell; argv lists are never built through a local shell.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from kallimachos.bridge.base import TransportError

Runner = Callable[[list[str]], tuple[int, str, str]]

#: ssh exits 255 when the connection itself fails.
_SSH_CONNECTION_FAILED = 255
#: rsync "partial transfer due to error" (used for a missing source file).
_RSYNC_PARTIAL = 23


def _subprocess_runner(argv: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=900)
    return proc.returncode, proc.stdout, proc.stderr


class SshRsyncTransport:
    def __init__(
        self,
        remote: str,
        remote_data_dir: str,
        *,
        ssh_options: Sequence[str],
        retry: int,
        runner: Runner | None = None,
        sleep: Callable[[float], None] = time.sleep,
        control_dir: Path | None = None,
    ) -> None:
        if not remote or remote.startswith("-") or any(c.isspace() for c in remote):
            raise TransportError(f"invalid bridge.remote: {remote!r}")
        self.remote = remote
        self.remote_data_dir = remote_data_dir
        self.ssh_options = list(ssh_options)
        self.retry = max(0, retry)
        self._run = runner or _subprocess_runner
        self._sleep = sleep
        self._control_dir = control_dir
        self._own_control_dir: Path | None = None
        self._open = False

    # -- connection -------------------------------------------------------------------------

    @property
    def control_path(self) -> Path:
        if self._control_dir is None:
            # short path: unix socket names are limited to ~100 bytes on macOS
            self._own_control_dir = Path(tempfile.mkdtemp(prefix="kb-"))
            self._control_dir = self._own_control_dir
        return self._control_dir / "cm"

    def _options(self, *, master: bool) -> list[str]:
        opts: list[str] = []
        for opt in self.ssh_options:
            opts += ["-o", opt]
        if not any(o.split("=", 1)[0].strip().lower() == "batchmode" for o in self.ssh_options):
            opts += ["-o", "BatchMode=yes"]
        opts += ["-o", f"ControlPath={self.control_path}"]
        opts += (
            ["-o", "ControlMaster=yes", "-o", "ControlPersist=no"]
            if master
            else ["-o", "ControlMaster=no"]
        )
        return opts

    def open(self) -> None:
        if self._open:
            return
        argv = ["ssh", *self._options(master=True), "-fN", self.remote]
        last = ""
        for attempt in range(self.retry + 1):
            if attempt:
                self._sleep(10.0)
            code, _out, err = self._run(argv)
            if code == 0:
                self._open = True
                return
            last = err.strip()[:300]
        raise TransportError(f"ssh master connection failed: {last}")

    def close(self) -> None:
        if self._open:
            self._run(["ssh", "-o", f"ControlPath={self.control_path}", "-O", "exit", self.remote])
            self._open = False
        if self._own_control_dir is not None:
            shutil.rmtree(self._own_control_dir, ignore_errors=True)
            self._own_control_dir = None
            self._control_dir = None

    def __enter__(self) -> SshRsyncTransport:
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- helpers ----------------------------------------------------------------------------

    def _remote_path(self, remote_relative: str) -> str:
        rel = remote_relative.lstrip("/")
        if ".." in rel.split("/"):
            raise TransportError(f"remote path must not contain '..': {remote_relative!r}")
        base = self.remote_data_dir.rstrip("/")
        return f"{base}/{rel}" if rel else base

    def _rsh(self) -> str:
        return shlex.join(["ssh", *self._options(master=False)])

    # -- Transport --------------------------------------------------------------------------

    def pull(
        self,
        remote_relative: str,
        local: Path,
        *,
        pattern: str | None = None,
        missing_ok: bool = False,
    ) -> None:
        self.open()
        src = f"{self.remote}:{shlex.quote(self._remote_path(remote_relative))}"
        # -r -t: recursive, keep mtimes; no -l, so remote symlinks are skipped, never followed
        argv = ["rsync", "-rt", "-e", self._rsh()]
        if remote_relative.endswith("/"):
            if pattern:
                argv += ["--include=*/", f"--include={pattern}", "--exclude=*"]
            local.mkdir(parents=True, exist_ok=True)
            dest = str(local) + "/"
        else:
            local.parent.mkdir(parents=True, exist_ok=True)
            dest = str(local)
        code, _out, err = self._run([*argv, src, dest])
        if code == 0:
            return
        if missing_ok and code == _RSYNC_PARTIAL and "No such file" in err:
            return
        raise TransportError(f"rsync pull failed ({code}): {err.strip()[:300]}")

    def push(self, local: Path, remote_relative: str) -> None:
        self.open()
        remote_path = self._remote_path(remote_relative)
        parent = remote_path.rsplit("/", 1)[0]
        code, _out, err = self.run(["mkdir", "-p", parent])
        if code != 0:
            raise TransportError(f"remote mkdir failed ({code}): {err.strip()[:300]}")
        dest = f"{self.remote}:{shlex.quote(remote_path)}"
        argv = ["rsync", "-t", "--ignore-existing", "-e", self._rsh(), str(local), dest]
        code, _out, err = self._run(argv)
        if code != 0:
            raise TransportError(f"rsync push failed ({code}): {err.strip()[:300]}")

    def run(self, argv: list[str]) -> tuple[int, str, str]:
        self.open()
        code, out, err = self._run(
            ["ssh", *self._options(master=False), self.remote, "--", shlex.join(argv)]
        )
        if code == _SSH_CONNECTION_FAILED:
            raise TransportError(f"ssh command failed: {err.strip()[:300]}")
        return code, out, err
