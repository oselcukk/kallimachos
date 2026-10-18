"""Offline :class:`~kallimachos.interfaces.HttpClient` for profile building.

``profile build`` without ``--live`` never opens a socket. It answers from recorded exchanges in
the ``tests/fixtures/README.md`` format (``<case>.json`` + ``<case>.body.<ext>``) found under the
directories named in ``KALLIMACHOS_HTTP_FIXTURES`` (``os.pathsep`` separated). Without fixtures
every request misses, and the build falls back to local-only proposals.

Every request is logged in :attr:`FixtureHttp.requests`; the privacy-gate tests assert that the
log stays empty for sensitive or person-name-like inputs.
"""

from __future__ import annotations

import json
import os
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from kallimachos.interfaces import HttpError, HttpResponse

ENV_FIXTURES = "KALLIMACHOS_HTTP_FIXTURES"


def _key(url: str, params: Mapping[str, str] | None) -> tuple[str, tuple[tuple[str, str], ...]]:
    return url, tuple(sorted((str(k), str(v)) for k, v in (params or {}).items()))


class FixtureHttp:
    """Replays recorded exchanges; unrecorded URLs return ``None`` and are listed in ``errors``."""

    def __init__(self, roots: Sequence[Path] = ()) -> None:
        self._cases: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[Path, dict[str, Any]]] = {}
        self._errors: list[HttpError] = []
        self.requests: list[str] = []
        for root in roots:
            self._load(Path(root))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> FixtureHttp:
        env = os.environ if env is None else env
        raw = env.get(ENV_FIXTURES, "")
        roots = [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]
        return cls(roots)

    def _load(self, root: Path) -> None:
        if not root.is_dir():
            return
        for case in sorted(root.rglob("*.json")):
            if case.name.endswith(".body.json"):
                continue
            try:
                data = json.loads(case.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or "request" not in data or "response" not in data:
                continue
            request = data["request"]
            key = _key(str(request.get("url", "")), request.get("params") or {})
            self._cases.setdefault(key, (case, data))

    @property
    def errors(self) -> list[HttpError]:
        return self._errors

    def recorded_params(self, url: str) -> list[dict[str, str]]:
        """The parameter sets recorded for ``url`` (any query), in file order; lets a replay fall
        back to "the recorded answer for this endpoint" when the exact query differs."""
        return [dict(params) for case_url, params in self._cases if case_url == url]

    @property
    def case_count(self) -> int:
        return len(self._cases)

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        tries: int = 3,
        timeout: float = 60.0,
        kind: str = "list",
        accept_status: Collection[int] = (),
    ) -> HttpResponse | None:
        query = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        self.requests.append(f"{url}?{query}" if query else url)
        found = self._cases.get(_key(url, params))
        host = urlsplit(url).hostname or url
        if found is None:
            self._errors.append(HttpError(host=host, message="no recorded fixture", url=url))
            return None
        case_path, data = found
        response = data["response"]
        body_file = response.get("body_file")
        body = b""
        if body_file:
            try:
                body = (case_path.parent / body_file).read_bytes()
            except OSError:
                self._errors.append(HttpError(host=host, message="fixture body missing", url=url))
                return None
        elif "body" in response:
            raw = response["body"]
            body = raw.encode("utf-8") if isinstance(raw, str) else json.dumps(raw).encode()
        return HttpResponse(
            url=url,
            status=int(response.get("status", 200)),
            headers={str(k): str(v) for k, v in (response.get("headers") or {}).items()},
            body=body,
            from_cache=True,
        )
