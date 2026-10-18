"""The one HTTP client every source and archive module uses.

Permanent rules (not configurable):

* per-host ``min_interval_s`` with floors from ``config.schema.DEFAULT_MIN_INTERVAL_S``;
* ``Retry-After`` honoured (seconds or HTTP date); 429 without it waits 30 s then 90 s, total
  extra wait capped at ``retry_after_cap_s`` (180 s);
* 5xx: ``5 * (attempt + 1)`` seconds; 400/401/403/404: no retry, first 64 KB of the body logged;
* ``Authorization`` / ``x-api-key`` never appear in URLs, cache keys or logs;
* ``OFFLINE=1`` serves only from cache / fixtures (tests).

Offline mode: the network transport is never used; a response comes from the cache, from an
injected ``transport`` or from the recorded exchanges named by ``KALLIMACHOS_HTTP_FIXTURES``
(:func:`fixture_transport_from_env`). Tests inject ``transport``, ``clock`` and ``sleep``.

:func:`from_config` is the one factory every command uses (``run``, ``archive``, ``sources``,
``process zotero``, ``profile build --live``): per-host intervals from the source registry and
the PDF hosts, the shared cache, the OpenAlex budget and the project user agent.

Writes (``POST``/``PATCH``, Zotero) go through :meth:`Http.request`: same pacing, same secret
rules (``Zotero-API-Key`` is a secret header), no cache, no budget, a single attempt by default.
"""

from __future__ import annotations

import dataclasses
import email.utils
import hashlib
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kallimachos.http.budget import BOOLEAN_OPERATOR_SLOW_LIMIT, Budget
from kallimachos.http.cache import Cache
from kallimachos.http.redact import SECRET_PARAMS, is_secret_param, redact_url
from kallimachos.interfaces import HttpError, HttpResponse

if TYPE_CHECKING:
    from kallimachos.config.schema import Config
    from kallimachos.paths import DataLayout

log = logging.getLogger("kallimachos.http")

SECRET_HEADERS = frozenset(
    {"authorization", "x-api-key", "proxy-authorization", "cookie", "zotero-api-key"}
)
"""Request headers that are sent but never logged, cached or put into a URL."""

__all__ = ["SECRET_HEADERS", "SECRET_PARAMS", "HostPolicy", "Http", "from_config", "redact_url"]


RETRY_429_DEFAULT_S: tuple[float, ...] = (30.0, 90.0)
SLOW_SEARCH_INTERVAL_S = 1.0
OFFLINE_ENV = "OFFLINE"

Transport = Callable[..., HttpResponse]
"""``(url, headers, timeout) -> HttpResponse`` for GET; writes call
``(url, headers, timeout, method, body)``. Raises :class:`TransportError` on network failure.
Non-2xx statuses are returned, not raised."""


class TransportError(Exception):
    """Connection-level failure (DNS, refused, timeout). Retried like a 5xx."""


def offline_from_env() -> bool:
    return os.environ.get(OFFLINE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


FIXTURES_ENV = "KALLIMACHOS_HTTP_FIXTURES"


def fixture_transport_from_env(env: Mapping[str, str] | None = None) -> Transport | None:
    """A transport replaying the recorded exchanges under the directories named in
    ``KALLIMACHOS_HTTP_FIXTURES`` (``os.pathsep`` separated; the ``tests/fixtures`` format), so a
    whole ``run`` or ``archive run`` can be produced without a network (``OFFLINE=1``). ``None``
    when the variable is empty or no case is found."""
    env = os.environ if env is None else env
    raw = env.get(FIXTURES_ENV, "")
    if not raw.strip():
        return None
    from kallimachos.profile.offline import FixtureHttp

    replay = FixtureHttp.from_env(env)
    if replay.case_count == 0:
        return None

    def transport(
        url: str,
        headers: Mapping[str, str],
        timeout: float,
        method: str = "GET",
        body: bytes | None = None,
    ) -> HttpResponse:
        parts = urllib.parse.urlsplit(url)
        base = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        params = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        public = {k: v for k, v in params.items() if not is_secret_param(k)}
        attempts = [params, public, *replay.recorded_params(base)[:1]]
        for attempt in attempts:
            resp = replay.get(base, params=attempt)
            if resp is not None:
                return dataclasses.replace(resp, url=url, from_cache=False)
        raise TransportError(f"no recorded fixture for {redact_url(url)}")

    return transport


def urllib_transport(
    url: str,
    headers: Mapping[str, str],
    timeout: float,
    method: str = "GET",
    body: bytes | None = None,
) -> HttpResponse:
    """Default transport (standard library only)."""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = resp.read()
            return HttpResponse(
                url=resp.geturl(),
                status=int(resp.status),
                headers={k: v for k, v in resp.headers.items()},
                body=body,
                elapsed_s=time.monotonic() - started,
            )
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except OSError:
            body = b""
        return HttpResponse(
            url=url,
            status=int(exc.code),
            headers={k: v for k, v in (exc.headers or {}).items()},
            body=body,
            elapsed_s=time.monotonic() - started,
        )
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise TransportError(f"{type(exc).__name__}: {reason}") from None


def _redacted(resp: HttpResponse) -> HttpResponse:
    """The same response with secret query values of its URL replaced by ``***``."""
    safe = redact_url(resp.url)
    return resp if safe == resp.url else dataclasses.replace(resp, url=safe)


def host_of(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def full_url(url: str, params: Mapping[str, str] | None) -> str:
    """URL with ``params`` appended (in the given order) to any query already present."""
    if not params:
        return url
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query.extend((str(k), str(v)) for k, v in params.items())
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def log_target(url: str) -> str:
    """What the log shows of a URL: scheme, host and path, never the query.

    Queries can carry search terms built from titles or author names
    (``privacy.personal_data_in_logs: false``); the redacted full URL is only logged at
    DEBUG level.
    """
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def cache_key(url: str, params: Mapping[str, str] | None) -> str:
    """sha1 of the canonical URL (sorted params). Secret-bearing params/headers are never part of
    it."""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if params:
        query.extend((str(k), str(v)) for k, v in params.items())
    kept = sorted((k, v) for k, v in query if not is_secret_param(k))
    canonical = urllib.parse.urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path or "/",
            urllib.parse.urlencode(kept),
            "",
        )
    )
    return hashlib.sha1(canonical.encode("utf-8"), usedforsecurity=False).hexdigest()


def parse_retry_after(value: str | None, now: float) -> float | None:
    """Seconds to wait from a ``Retry-After`` header (delta-seconds or HTTP-date)."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return max(float(text), 0.0)
    except ValueError:
        pass
    try:
        moment = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    return max(moment.timestamp() - now, 0.0)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lname = name.lower()
    for k, v in headers.items():
        if k.lower() == lname:
            return v
    return None


@dataclass
class HostPolicy:
    """Per-host pacing. ``min_interval_s`` keys are host names; missing hosts use
    ``default_interval_s``."""

    min_interval_s: dict[str, float] = field(default_factory=dict)
    default_interval_s: float = 0.5
    retry_after_cap_s: float = 180.0
    max_error_body_bytes: int = 64 * 1024

    def interval_for(self, host: str) -> float:
        if host in self.min_interval_s:
            return self.min_interval_s[host]
        # "api.example.org" also matches a policy entry for "example.org"
        for name, value in self.min_interval_s.items():
            if host.endswith("." + name):
                return value
        return self.default_interval_s


class Http:
    """Implements :class:`kallimachos.interfaces.HttpClient`."""

    def __init__(
        self,
        policy: HostPolicy,
        cache: Cache | None,
        budget: Budget | None,
        ua: str,
        *,
        offline: bool = False,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        budget_hosts: Collection[str] = ("api.openalex.org",),
        weekly: bool = False,
    ) -> None:
        self.policy = policy
        self.cache = cache
        self.budget = budget
        self.ua = ua
        self.offline = offline or offline_from_env()
        self._errors: list[HttpError] = []
        self._transport: Transport | None = transport
        if transport is None and not self.offline:
            self._transport = urllib_transport
        self._clock = clock
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._last_request: dict[str, float] = {}
        self.budget_hosts = frozenset(h.lower() for h in budget_hosts)
        self.weekly = weekly
        self.requests_by_host: dict[str, int] = {}

    # -- internals -----------------------------------------------------------------------------

    def _error(self, host: str, message: str, *, status: int | None, url: str) -> None:
        err = HttpError(host=host, message=message, status=status, url=redact_url(url))
        self._errors.append(err)
        log.warning("%s (%s)", message, log_target(url))

    def _interval(self, host: str, params: Mapping[str, str] | None, kind: str) -> float:
        interval = self.policy.interval_for(host)
        if params and kind == "search":
            ops = sum(
                Budget.boolean_operator_count(str(v))
                for k, v in params.items()
                if k.lower() in ("search", "filter", "query", "q")
            )
            if ops > BOOLEAN_OPERATOR_SLOW_LIMIT:
                interval = max(interval, SLOW_SEARCH_INTERVAL_S)
        return interval

    def _pace(self, host: str, interval: float) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = interval - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._clock()

    def _headers(self, headers: Mapping[str, str] | None) -> dict[str, str]:
        out = {"User-Agent": self.ua, "Accept-Encoding": "identity"}
        for k, v in (headers or {}).items():
            out[str(k)] = str(v)
        return out

    def _budgeted(self, host: str, kind: str) -> bool:
        return self.budget is not None and host in self.budget_hosts and kind != "pdf"

    # -- public API ----------------------------------------------------------------------------

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
        """GET with pacing, retries, cache and budget. Returns ``None`` after ``tries`` failures and
        records an :class:`HttpError`. ``kind`` (``list`` | ``search`` | ``single`` | ``pdf``)
        selects the cache TTL and the OpenAlex unit cost.

        A 4xx answer (other than 429) is not retried: ``None`` is returned and the error message
        carries ``HTTP <status>`` plus a short single-line excerpt of the body. A status listed in
        ``accept_status`` is returned as a normal response instead (not cached, no error), so a
        caller can read a documented error body such as zbMATH's ``noRecordsMatch`` (HTTP 400).
        """
        return self.request(
            "GET",
            url,
            params=params,
            headers=headers,
            tries=tries,
            timeout=timeout,
            kind=kind,
            accept_status=accept_status,
        )

    def post(
        self,
        url: str,
        *,
        body: bytes,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        tries: int = 1,
        timeout: float = 60.0,
        accept_status: Collection[int] = (),
    ) -> HttpResponse | None:
        """POST ``body``; one attempt by default (writes are not idempotent). Never cached or
        budgeted."""
        return self.request(
            "POST",
            url,
            params=params,
            headers=headers,
            body=body,
            tries=tries,
            timeout=timeout,
            kind="write",
            accept_status=accept_status,
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        tries: int = 3,
        timeout: float = 60.0,
        kind: str = "list",
        accept_status: Collection[int] = (),
    ) -> HttpResponse | None:
        """Any method. GET uses the cache and the budget; other methods never do. Secret headers
        (``SECRET_HEADERS``) and secret query parameters never reach logs, cache keys or error
        records whatever the method."""
        method = method.upper()
        is_get = method == "GET" and body is None
        host = host_of(url)
        target = full_url(url, params)
        safe = log_target(target)
        key = cache_key(url, params)
        accepted = frozenset(int(x) for x in accept_status)

        if is_get and self.cache is not None:
            cached = self.cache.get(key, kind)
            if cached is not None:
                log.debug("cache hit %s", safe)
                return cached

        transport = self._transport
        if transport is None:
            self._error(host, f"{host}: offline and not cached", status=None, url=target)
            return None

        budgeted = is_get and self._budgeted(host, kind)
        budget = self.budget
        if budgeted and budget is not None and not budget.can_spend(kind, weekly=self.weekly):
            self._error(host, f"{host}: budget exhausted", status=None, url=target)
            return None

        send_headers = self._headers(headers)
        interval = self._interval(host, params, kind)
        extra_wait = 0.0
        n429 = 0
        attempts = max(int(tries), 1)
        last_status: int | None = None
        for attempt in range(attempts):
            self._pace(host, interval)
            self.requests_by_host[host] = self.requests_by_host.get(host, 0) + 1
            log.info("%s %s (attempt %d/%d)", method, safe, attempt + 1, attempts)
            log.debug("%s %s", method, redact_url(target))
            try:
                if is_get:
                    resp = transport(target, send_headers, timeout)
                else:
                    resp = transport(target, send_headers, timeout, method, body)
            except TransportError as exc:
                last_status = None
                log.warning("%s: %s", host, exc)
                if attempt + 1 < attempts:
                    self._sleep(5.0 * (attempt + 1))
                continue
            status = resp.status
            last_status = status

            resp = _redacted(resp)  # the response URL never carries a key or an e-mail onwards
            if 200 <= status < 400:
                if budgeted and budget is not None:
                    budget.record(kind, resp.headers, meta_cost_usd=Budget.meta_cost_usd(resp.body))
                if is_get and self.cache is not None and status == 200:
                    self.cache.put(key, kind, resp)
                return resp

            if status in accepted:
                log.info("%s HTTP %d for %s accepted by the caller", host, status, safe)
                return resp

            if status == 429:
                n429 += 1
                if budgeted and budget is not None:
                    budget.record(kind, resp.headers)
                    if Budget.exhausted_from(resp.headers):
                        self._error(
                            host, f"{host} HTTP 429: budget exhausted", status=429, url=target
                        )
                        return None
                wait = parse_retry_after(_header(resp.headers, "Retry-After"), self._wall_clock())
                if wait is None and budgeted and budget is not None:
                    wait = budget.reset_wait_s(resp.headers)
                if wait is None:
                    wait = RETRY_429_DEFAULT_S[min(n429 - 1, len(RETRY_429_DEFAULT_S) - 1)]
                if attempt + 1 >= attempts:
                    break
                if extra_wait + wait > self.policy.retry_after_cap_s:
                    self._error(
                        host,
                        f"{host} HTTP 429: wait {wait:.0f} s exceeds the "
                        f"{self.policy.retry_after_cap_s:.0f} s cap",
                        status=429,
                        url=target,
                    )
                    return None
                extra_wait += wait
                log.warning("%s HTTP 429; waiting %.1f s", host, wait)
                self._sleep(wait)
                continue

            if 400 <= status < 500:
                excerpt = resp.body[: self.policy.max_error_body_bytes]
                text = excerpt.decode("utf-8", errors="replace")
                log.warning("%s HTTP %d for %s; body: %s", host, status, safe, text)
                short = " ".join(text.split())[:300]
                message = f"{host} HTTP {status}" + (f": {short}" if short else "")
                self._error(host, message, status=status, url=target)
                return None

            # 5xx and anything else unexpected: back off and retry
            log.warning("%s HTTP %d for %s", host, status, safe)
            if attempt + 1 < attempts:
                self._sleep(5.0 * (attempt + 1))

        if last_status == 429:
            self._error(host, f"{host} HTTP 429", status=429, url=target)
        else:
            self._error(host, f"{host}: {attempts} tries failed", status=last_status, url=target)
        return None

    @property
    def errors(self) -> list[HttpError]:
        return self._errors


# ---------------------------------------------------------------------------
# the one factory
# ---------------------------------------------------------------------------

PDF_HOST_INTERVALS_S: dict[str, float] = {
    "arxiv.org": 3.2,
    "export.arxiv.org": 3.1,
    "eprint.iacr.org": 1.0,
}
"""Hosts the archive downloads PDFs from; never faster than this."""


def from_config(
    config: Config,
    layout: DataLayout,
    *,
    weekly: bool = False,
    with_budget: bool = True,
    with_cache: bool = True,
    default_interval_s: float = 0.5,
    offline: bool | None = None,
    transport: Transport | None = None,
) -> Http:
    """The shared client for a command: per-host intervals from ``sources.registry.host_intervals``
    (configured ``min_interval_s`` never below the built-in floors) plus the PDF hosts, the HTTP
    cache under ``layout.http_cache``, the OpenAlex :class:`Budget`
    (``state/budget/openalex.jsonl``) and the project user agent. ``offline=None`` reads
    ``OFFLINE=1``.
    """
    from kallimachos.http.cache import Cache
    from kallimachos.http.ua import user_agent
    from kallimachos.interfaces import dump
    from kallimachos.sources.registry import host_intervals

    intervals = dict(PDF_HOST_INTERVALS_S)
    for host, value in host_intervals(config).items():
        intervals[host] = max(value, intervals.get(host, 0.0))
    policy = HostPolicy(min_interval_s=intervals, default_interval_s=default_interval_s)
    budget: Budget | None = None
    if with_budget:
        oa = config.sources.openalex
        unit = dump(oa.unit_cost_usd)
        assert isinstance(unit, dict)
        budget = Budget(
            layout.budget_dir / "openalex.jsonl",
            daily_budget_usd=oa.daily_budget_usd,
            weekly_extra_usd=oa.weekly_extra_usd,
            daily_cap_usd=oa.daily_cap_usd,
            unit_cost_usd={str(k): float(v) for k, v in unit.items()},
        )
    is_offline = offline_from_env() if offline is None else offline
    if transport is None and is_offline:
        transport = fixture_transport_from_env()
    return Http(
        policy,
        Cache(layout.http_cache) if with_cache else None,
        budget,
        user_agent(),
        offline=is_offline,
        transport=transport,
        weekly=weekly,
    )
