"""HTTP response cache under ``cache/http/<sha1>.json`` with per-kind TTL.

An entry stores the response status, headers and body (base64) plus ``stored_at`` and ``kind``.
Request headers are never stored, so ``Authorization`` / ``x-api-key`` cannot leak into the
cache; the key itself comes from :func:`kallimachos.http.client.cache_key`, which drops
secret-bearing parameters, and the stored ``url`` is redacted (``api_key=***``) because the
transport reports the final URL with its query. ``doctor`` flags and ``doctor --fix`` removes
entries written before that rule (:func:`entries_with_secrets`).
"""

from __future__ import annotations

import base64
import contextlib
import json
import re
import time
from collections.abc import Callable
from pathlib import Path

from kallimachos.http.redact import redact_url
from kallimachos.interfaces import HttpResponse
from kallimachos.state import store

DEFAULT_TTL_S: dict[str, int] = {
    "single": 30 * 86400,  # metadata: 30 days
    "list": 86400,  # searches/lists: 1 day
    "search": 86400,
    "pdf": 0,  # never cached
}

_KEY_RE = re.compile(r"^[0-9a-f]{40}$")
_DROP_HEADERS = frozenset({"set-cookie", "authorization", "x-api-key", "proxy-authorization"})


class Cache:
    def __init__(
        self,
        directory: Path,
        ttl_s: dict[str, int] | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.directory = directory
        self.ttl_s = dict(DEFAULT_TTL_S if ttl_s is None else ttl_s)
        self.clock = clock

    def ttl_for(self, kind: str) -> int:
        """TTL in seconds; unknown kinds use the ``list`` TTL."""
        return int(self.ttl_s.get(kind, self.ttl_s.get("list", 0)))

    def _path(self, key: str) -> Path:
        if not _KEY_RE.match(key):
            raise ValueError(f"cache key must be a sha1 hex digest, got {key!r}")
        return self.directory / f"{key}.json"

    def get(self, key: str, kind: str) -> HttpResponse | None:
        """Return a cached response younger than the TTL, else ``None``."""
        ttl = self.ttl_for(kind)
        if ttl <= 0:
            return None
        path = self._path(key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            stored_at = float(data["stored_at"])
            if self.clock() - stored_at > ttl:
                return None
            return HttpResponse(
                url=str(data["url"]),
                status=int(data["status"]),
                headers={str(k): str(v) for k, v in dict(data.get("headers", {})).items()},
                body=base64.b64decode(data["body_b64"]),
                from_cache=True,
            )
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError, OSError):
            return None  # a corrupt entry is a miss; the next put replaces it

    def put(self, key: str, kind: str, response: HttpResponse) -> None:
        if self.ttl_for(kind) <= 0:
            return
        headers = {k: v for k, v in response.headers.items() if k.lower() not in _DROP_HEADERS}
        entry = {
            "stored_at": self.clock(),
            "kind": kind,
            "url": redact_url(response.url),  # api_key / email / mailto never rest on disk
            "status": response.status,
            "headers": headers,
            "body_b64": base64.b64encode(response.body).decode("ascii"),
        }
        store.write_json_atomic(self._path(key), entry)

    def entries_with_secrets(self) -> list[Path]:
        """Cache files whose stored URL still carries a secret query value (old entries)."""
        from kallimachos.http.redact import carries_secret

        if not self.directory.is_dir():
            return []
        found: list[Path] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                url = str(data.get("url", ""))
            except (ValueError, OSError, AttributeError):
                continue
            if carries_secret(url):
                found.append(path)
        return found

    def purge_expired(self) -> int:
        """Delete expired entries; return the count."""
        if not self.directory.is_dir():
            return 0
        now = self.clock()
        removed = 0
        for path in self.directory.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                expired = now - float(data["stored_at"]) > self.ttl_for(str(data.get("kind")))
            except (ValueError, KeyError, TypeError, OSError):
                expired = True
            if expired:
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                    removed += 1
        return removed
