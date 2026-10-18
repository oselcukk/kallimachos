"""Test doubles shared by several test modules.

* :class:`FixtureHttp` - a :class:`kallimachos.interfaces.HttpClient` that answers from recorded
  fixtures (``tests/fixtures/<source>/<case>.json``, format in ``tests/fixtures/README.md``) or
  from responses added in code, and raises :class:`UnrecordedRequest` for anything else. It keeps
  a request log (``requests``) so privacy tests can assert that nothing was sent.
* :class:`ScriptedTransport` - a transport for :class:`kallimachos.http.client.Http` that replays
  a fixed sequence of responses (retry / 429 / budget tests).
* :class:`FakeClock` - monotonic + wall clock with a recording ``sleep``.

Requests are matched with :func:`kallimachos.http.client.cache_key`, i.e. on the canonical URL
with sorted parameters and *without* secret parameters (``api_key``, ``email``, ``mailto`` ...),
exactly like the real cache.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kallimachos.http.client import cache_key, host_of, redact_url
from kallimachos.interfaces import HttpError, HttpResponse

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


class UnrecordedRequest(AssertionError):
    """A test sent a request that no fixture answers."""


@dataclass
class RecordedRequest:
    url: str
    params: dict[str, str]
    headers: dict[str, str]
    kind: str


def _body_bytes(body: bytes | str | Mapping[str, Any] | list[Any] | None) -> bytes:
    if body is None:
        return b""
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


TRANSPORT_FAILURE = 0
"""``status`` of a registered response that stands for a connection failure (``None`` + ``"<host>:
<tries> tries failed"``, like the real client after its retries)."""


@dataclass
class _Entry:
    responses: list[HttpResponse] = field(default_factory=list)
    served: int = 0

    def next(self, *, repeat_last: bool, url: str) -> HttpResponse:
        if self.served >= len(self.responses) and not repeat_last:
            raise UnrecordedRequest(f"no more responses recorded for {redact_url(url)}")
        idx = min(self.served, len(self.responses) - 1)
        self.served += 1
        return self.responses[idx]


class FixtureHttp:
    """Fixture-backed ``HttpClient`` (the one fake shared by all tests).

    Responses are registered for an exact request (:meth:`add`, canonical cache key) or for a URL
    prefix (:meth:`add_prefix`, longest prefix wins). Non-2xx answers behave like the real client:
    ``None`` plus an :class:`HttpError` (``"<host> HTTP <status>: <body excerpt>"``), unless the
    status is in ``accept_status``. ``repeat_last=False`` makes every registered response serve
    exactly once (paging tests). Every request is logged in ``requests``.
    """

    def __init__(self, *sources: str | Path, strict: bool = True, repeat_last: bool = True) -> None:
        self._entries: dict[str, _Entry] = {}
        self._prefixes: dict[str, _Entry] = {}
        self._errors: list[HttpError] = []
        self.requests: list[RecordedRequest] = []
        self.strict = strict
        self.repeat_last = repeat_last
        for source in sources:
            self.load(source)

    # -- building --------------------------------------------------------------------------------

    def add(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
        body: bytes | str | Mapping[str, Any] | list[Any] | None = None,
    ) -> FixtureHttp:
        """Register one response; several ``add`` calls for the same request are served in
        order (the last one repeats)."""
        key = cache_key(url, params)
        response = HttpResponse(
            url=url, status=status, headers=dict(headers or {}), body=_body_bytes(body)
        )
        self._entries.setdefault(key, _Entry()).responses.append(response)
        return self

    def add_prefix(self, prefix: str, *responses: HttpResponse) -> FixtureHttp:
        """Register responses for every URL starting with ``prefix`` (served in order)."""
        self._prefixes.setdefault(prefix, _Entry()).responses.extend(responses)
        return self

    def load(self, source: str | Path) -> FixtureHttp:
        """Load every ``<case>.json`` of a fixture folder (a name under ``tests/fixtures`` or a
        path), or a single case file."""
        path = Path(source)
        if not path.is_absolute() and not path.exists():
            path = FIXTURES / path
        files: Iterable[Path]
        if path.is_dir():
            files = sorted(p for p in path.glob("*.json") if ".body." not in p.name)
        else:
            files = [path]
        for case in files:
            self._load_case(case)
        return self

    def _load_case(self, case: Path) -> None:
        data = json.loads(case.read_text(encoding="utf-8"))
        request = data["request"]
        response = data.get("response", {})
        if "body_file" in response:
            body: bytes | str | None = (case.parent / response["body_file"]).read_bytes()
        else:
            body = response.get("body")
            if body is not None and not isinstance(body, str):
                body = json.dumps(body, ensure_ascii=False)
        self.add(
            request["url"],
            params=request.get("params"),
            status=int(response.get("status", 200)),
            headers=response.get("headers"),
            body=body,
        )

    # -- HttpClient ------------------------------------------------------------------------------

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
        self.requests.append(
            RecordedRequest(
                url=url,
                params={str(k): str(v) for k, v in (params or {}).items()},
                headers={str(k): str(v) for k, v in (headers or {}).items()},
                kind=kind,
            )
        )
        entry = self._entries.get(cache_key(url, params))
        if entry is None:
            matches = [p for p in self._prefixes if url.startswith(p)]
            if matches:
                entry = self._prefixes[max(matches, key=len)]
        host = host_of(url)
        if entry is None:
            if self.strict:
                raise UnrecordedRequest(f"no fixture for GET {redact_url(url)} params={params}")
            self._errors.append(HttpError(host, f"{host}: no fixture", None, redact_url(url)))
            return None
        resp = entry.next(repeat_last=self.repeat_last, url=url)
        if (200 <= resp.status < 400) or resp.status in accept_status:
            return HttpResponse(
                url=resp.url or url, status=resp.status, headers=dict(resp.headers), body=resp.body
            )
        excerpt = " ".join(resp.body[:65536].decode("utf-8", "replace").split())[:300]
        status: int | None = resp.status
        if resp.status == TRANSPORT_FAILURE:
            message = f"{host}: {tries} tries failed"
            status = None
        elif resp.status == 429:
            message = f"{host} HTTP 429"
        elif 400 <= resp.status < 500:
            message = f"{host} HTTP {resp.status}" + (f": {excerpt}" if excerpt else "")
        else:
            message = f"{host}: {tries} tries failed"
        self._errors.append(HttpError(host, message, status, redact_url(url)))
        return None

    @property
    def errors(self) -> list[HttpError]:
        return self._errors

    # -- assertions ------------------------------------------------------------------------------

    def urls(self) -> list[str]:
        return [r.url for r in self.requests]

    def unused(self) -> list[str]:
        """Cache keys of registered responses that were never requested."""
        return [k for k, e in self._entries.items() if e.served == 0]


class FakeClock:
    """``monotonic`` / ``time`` replacement; ``sleep`` advances both and is recorded."""

    def __init__(self, start: float = 1_000_000.0, wall: float = 1_760_000_000.0) -> None:
        self.mono = start
        self.wall = wall
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return self.wall

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += seconds


class ScriptedTransport:
    """Replays responses (or exceptions) in order; records every call."""

    def __init__(self, *responses: HttpResponse | BaseException) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str], float]] = []
        self.call_times: list[float] = []
        self.clock: FakeClock | None = None

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> HttpResponse:
        self.calls.append((url, dict(headers), timeout))
        if self.clock is not None:
            self.call_times.append(self.clock.monotonic())
        if not self.responses:
            raise AssertionError(f"unexpected request {url}")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def response(
    status: int = 200,
    body: bytes | str | Mapping[str, Any] | list[Any] | None = b"",
    headers: Mapping[str, str] | None = None,
    url: str = "https://api.example.org/x",
) -> HttpResponse:
    return HttpResponse(url=url, status=status, headers=dict(headers or {}), body=_body_bytes(body))


class FixtureTransport:
    """A :data:`kallimachos.http.client.Transport` answering URL prefixes from recorded responses,
    so the *real* ``Http`` (pacing, cache, budget, secret rules) runs in integration tests without
    a network. Unknown URLs raise :class:`UnrecordedRequest`; every call is logged."""

    def __init__(self, *routes: tuple[str, HttpResponse]) -> None:
        self.routes: dict[str, HttpResponse] = dict(routes)
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def add(self, prefix: str, response: HttpResponse) -> FixtureTransport:
        self.routes[prefix] = response
        return self

    def __call__(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
        method: str = "GET",
        body: bytes | None = None,
    ) -> HttpResponse:
        self.calls.append((method, url, dict(headers)))
        matches = [p for p in self.routes if url.startswith(p)]
        if not matches:
            raise UnrecordedRequest(f"no recorded response for {method} {redact_url(url)}")
        resp = self.routes[max(matches, key=len)]
        return HttpResponse(url=url, status=resp.status, headers=dict(resp.headers), body=resp.body)
