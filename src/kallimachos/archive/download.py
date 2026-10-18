"""PDF download.

``Accept: application/pdf``, 120 s, size cap, ``%PDF`` signature, ``pdfinfo`` pages > 0 when
available, sha256; written with ``O_EXCL`` (never overwrite), ``_<sha8>`` suffix on name clash.
Only open-access URLs proposed by :mod:`kallimachos.archive.oa` reach this module; there is no
authenticated or paywalled route.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from kallimachos.interfaces import HttpClient, OaCandidate

HOST_INTERVALS_S: dict[str, float] = {"arxiv.org": 3.2, "api.openalex.org": 0.15}
DEFAULT_INTERVAL_S = 2.0
TIMEOUT_S = 120.0

FailureKind = Literal[
    "forbidden", "html", "not_pdf", "rate_limited", "server", "network", "too_large", "empty"
]
NOT_RETRYABLE: frozenset[str] = frozenset({"forbidden", "html", "not_pdf", "too_large"})


class DownloadError(Exception):
    """A candidate URL did not yield a PDF. ``kind`` drives the retry policy."""

    def __init__(self, kind: FailureKind, message: str, *, url: str, status: int | None = None):
        super().__init__(f"{kind}: {message}")
        self.kind: FailureKind = kind
        self.message = message
        self.url = url
        self.status = status

    @property
    def host(self) -> str:
        return host_of(self.url)

    @property
    def retryable(self) -> bool:
        return self.kind not in NOT_RETRYABLE


@dataclass
class DownloadResult:
    path: Path
    sha256: str
    size: int
    pages: int | None
    oa: OaCandidate


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def interval_for(url: str) -> float:
    host = host_of(url)
    for known, interval in HOST_INTERVALS_S.items():
        if host == known or host.endswith("." + known):
            return interval
    return DEFAULT_INTERVAL_S


def is_pdf(head: bytes) -> bool:
    return head.startswith(b"%PDF")


def pdf_pages(path: Path) -> int | None:
    """Page count from ``pdfinfo``; ``None`` when the tool is not installed or fails."""
    tool = shutil.which("pdfinfo")
    if tool is None:
        return None
    try:
        out = subprocess.run(
            [tool, str(path)], capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"^Pages:\s+(\d+)", out.stdout, re.MULTILINE)
    return int(m.group(1)) if m else (0 if out.returncode != 0 else None)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download_pdf(
    candidate: OaCandidate, destination: Path, http: HttpClient, *, max_mb: int
) -> DownloadResult:
    """Download to ``destination`` (final name decided by the caller).

    Raises on non-PDF/HTML/403.
    """
    url = candidate.url
    resp = http.get(
        url, headers={"Accept": "application/pdf"}, tries=1, timeout=TIMEOUT_S, kind="pdf"
    )
    if resp is None:
        status = next((e.status for e in reversed(http.errors) if e.url == url), None)
        raise (
            _status_error(url, status)
            if status
            else DownloadError("network", "no response", url=url)
        )
    if resp.status >= 400:
        raise _status_error(url, resp.status)
    limit = max_mb * 1024 * 1024
    declared = _header(resp.headers, "content-length")
    if (declared and declared.isdigit() and int(declared) > limit) or len(resp.body) > limit:
        raise DownloadError("too_large", f"larger than {max_mb} MB", url=url)
    if not resp.body:
        raise DownloadError("empty", "empty body", url=url, status=resp.status)
    if not is_pdf(resp.body[:5]):
        ctype = (_header(resp.headers, "content-type") or "unknown").split(";")[0].strip()
        looks_html = "html" in ctype or b"<html" in resp.body[:2048].lower()
        kind: FailureKind = "html" if looks_html else "not_pdf"
        raise DownloadError(kind, f"not a PDF ({ctype})", url=url, status=resp.status)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as fh:
        fh.write(resp.body)
    pages = pdf_pages(destination)
    if pages == 0:
        destination.unlink()
        raise DownloadError("not_pdf", "pdfinfo reports 0 pages", url=url)
    digest = hashlib.sha256(resp.body).hexdigest()
    return DownloadResult(destination, digest, len(resp.body), pages, candidate)


def _header(headers: dict[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _status_error(url: str, status: int | None) -> DownloadError:
    if status in (401, 403):
        return DownloadError("forbidden", f"HTTP {status}", url=url, status=status)
    if status == 429:
        return DownloadError("rate_limited", "HTTP 429", url=url, status=status)
    if status is not None and status >= 500:
        return DownloadError("server", f"HTTP {status}", url=url, status=status)
    return DownloadError("not_pdf", f"HTTP {status}", url=url, status=status)


def write_exclusive(source_tmp: Path, target: Path) -> Path:
    """Move into place with ``O_EXCL``; on clash with
    different content append ``_<sha8>``.

    Never overwrites: the target is created with a hard link (atomic, fails if it exists) or an
    ``O_EXCL`` copy where links are not supported. Identical content already at the target (or at
    the ``_<sha8>`` name) is reused and the temporary file is removed.
    """
    digest = sha256_file(source_tmp)
    target.parent.mkdir(parents=True, exist_ok=True)
    candidates = [target, target.with_name(f"{target.stem}_{digest[:8]}{target.suffix}")]
    for path in candidates:
        if _place(source_tmp, path):
            source_tmp.unlink()
            return path
        if path.is_file() and not path.is_symlink() and sha256_file(path) == digest:
            source_tmp.unlink()
            return path
    raise FileExistsError(
        f"both {candidates[0].name} and {candidates[1].name} exist with other content"
    )


def _place(src: Path, dst: Path) -> bool:
    """Create ``dst`` with ``src``'s content without ever replacing an existing file."""
    try:
        os.link(src, dst)
        return True
    except FileExistsError:
        return False
    except OSError:
        pass
    try:
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "wb") as out, src.open("rb") as inp:
        shutil.copyfileobj(inp, out)
    return True


class HostPacer:
    """Per-host spacing for PDF requests and the unreachable retry policy.

    * every host keeps its interval (arxiv.org 3.2 s, OpenAlex 0.15 s, others 2.0 s);
    * a host that answered 429 is spaced by at least ``slow_interval_s`` (20 s) from then on;
    * after ``max_consecutive_429`` consecutive 429 answers the host is dropped for this run.

    ``clock`` and ``sleep`` are injectable for tests.
    """

    def __init__(
        self,
        *,
        slow_interval_s: float = 20.0,
        max_consecutive_429: int = 2,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.slow_interval_s = slow_interval_s
        self.max_consecutive_429 = max_consecutive_429
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self._slow: set[str] = set()
        self._streak: dict[str, int] = {}
        self.dropped: set[str] = set()

    def allowed(self, url: str) -> bool:
        return host_of(url) not in self.dropped

    def wait(self, url: str) -> None:
        host = host_of(url)
        interval = interval_for(url)
        if host in self._slow:
            interval = max(interval, self.slow_interval_s)
        last = self._last.get(host)
        if last is not None:
            remaining = last + interval - self._clock()
            if remaining > 0:
                self._sleep(remaining)
        self._last[host] = self._clock()

    def record(self, url: str, status: int | None) -> None:
        host = host_of(url)
        if status == 429:
            self._slow.add(host)
            self._streak[host] = self._streak.get(host, 0) + 1
            if self._streak[host] >= self.max_consecutive_429:
                self.dropped.add(host)
        else:
            self._streak[host] = 0
