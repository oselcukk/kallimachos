"""http.client: pacing, Retry-After, 429/4xx/5xx policy, secrets, cache, offline."""

from __future__ import annotations

import email.utils
import logging
from pathlib import Path

import pytest

from kallimachos.http.cache import Cache
from kallimachos.http.client import (
    HostPolicy,
    Http,
    TransportError,
    cache_key,
    parse_retry_after,
    redact_url,
)
from kallimachos.http.ua import crossref_user_agent, user_agent
from kallimachos.interfaces import HttpClient
from tests.unit.fakes import FakeClock, ScriptedTransport, response

URL = "https://api.example.org/works"


def make_http(
    transport: ScriptedTransport,
    clock: FakeClock,
    *,
    cache: Cache | None = None,
    policy: HostPolicy | None = None,
    offline: bool = False,
) -> Http:
    transport.clock = clock
    return Http(
        policy or HostPolicy(min_interval_s={"api.example.org": 1.0}),
        cache,
        None,
        user_agent(),
        offline=offline,
        transport=transport,
        clock=clock.monotonic,
        wall_clock=clock.time,
        sleep=clock.sleep,
    )


def test_http_implements_protocol() -> None:
    clock = FakeClock()
    assert isinstance(make_http(ScriptedTransport(), clock), HttpClient)


def test_success_sends_user_agent() -> None:
    clock = FakeClock()
    t = ScriptedTransport(response(200, b"ok"))
    http = make_http(t, clock)
    resp = http.get(URL, params={"q": "x"})
    assert resp is not None and resp.body == b"ok"
    url, headers, _ = t.calls[0]
    assert url == URL + "?q=x"
    assert headers["User-Agent"].startswith("kallimachos/")
    assert "github.com/oselcukk/kallimachos" in headers["User-Agent"]
    assert http.errors == []


def test_per_host_interval_measured_with_fake_clock() -> None:
    clock = FakeClock()
    t = ScriptedTransport(*[response(200, b"x") for _ in range(3)], response(200, b"y"))
    http = make_http(t, clock, policy=HostPolicy(min_interval_s={"api.example.org": 3.1}))
    for _ in range(3):
        http.get(URL, params={"page": str(len(t.calls))})
        clock.advance(0.5)  # caller work between requests
    gaps = [b - a for a, b in zip(t.call_times, t.call_times[1:], strict=False)]
    assert all(g >= 3.1 - 1e-9 for g in gaps), gaps
    # a different host is not delayed by the first one
    sleeps_before = len(clock.sleeps)
    http.get("https://other.example.net/x")
    assert len(clock.sleeps) == sleeps_before


def test_unknown_host_uses_default_interval_and_suffix_match() -> None:
    policy = HostPolicy(min_interval_s={"example.org": 2.0}, default_interval_s=0.5)
    assert policy.interval_for("api.example.org") == 2.0
    assert policy.interval_for("example.net") == 0.5


def test_retry_after_seconds_honoured() -> None:
    clock = FakeClock()
    t = ScriptedTransport(response(429, headers={"Retry-After": "7"}), response(200, b"ok"))
    http = make_http(t, clock)
    assert http.get(URL) is not None
    assert 7 in clock.sleeps


def test_retry_after_http_date_honoured() -> None:
    clock = FakeClock()
    when = email.utils.formatdate(clock.time() + 42, usegmt=True)
    t = ScriptedTransport(response(429, headers={"Retry-After": when}), response(200, b"ok"))
    http = make_http(t, clock)
    assert http.get(URL) is not None
    assert any(41 <= s <= 42 for s in clock.sleeps), clock.sleeps


def test_parse_retry_after_variants() -> None:
    assert parse_retry_after("12", 0) == 12
    assert parse_retry_after("", 0) is None
    assert parse_retry_after(None, 0) is None
    assert parse_retry_after("garbage", 0) is None
    past = email.utils.formatdate(1000, usegmt=True)
    assert parse_retry_after(past, 5000) == 0


def test_429_without_retry_after_waits_30_then_90() -> None:
    clock = FakeClock()
    t = ScriptedTransport(response(429), response(429), response(200, b"ok"))
    http = make_http(t, clock)
    assert http.get(URL) is not None
    waits = [s for s in clock.sleeps if s >= 30]
    assert waits == [30, 90]


def test_429_total_extra_wait_capped_at_180() -> None:
    clock = FakeClock()
    t = ScriptedTransport(*[response(429, headers={"Retry-After": "100"}) for _ in range(5)])
    http = make_http(t, clock)
    assert http.get(URL, tries=5) is None
    assert sum(s for s in clock.sleeps if s >= 30) <= 180
    assert len(t.calls) == 2  # 100 s waited once; a second 100 s would exceed the cap
    assert "429" in http.errors[-1].message


def test_429_exhausting_tries_records_host_error() -> None:
    clock = FakeClock()
    t = ScriptedTransport(response(429), response(429), response(429))
    http = make_http(t, clock)
    assert http.get(URL) is None
    assert http.errors[-1].message == "api.example.org HTTP 429"
    assert http.errors[-1].status == 429


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_4xx_single_try_body_logged(status: int, caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    body = b"<error>noRecordsMatch</error>" + b"z" * (70 * 1024)
    t = ScriptedTransport(response(status, body))
    http = make_http(t, clock)
    with caplog.at_level(logging.WARNING, logger="kallimachos.http"):
        assert http.get(URL) is None
    assert len(t.calls) == 1
    err = http.errors[-1]
    assert err.status == status
    assert err.message.startswith(f"api.example.org HTTP {status}")
    assert "noRecordsMatch" in err.message
    body_logs = [r.getMessage() for r in caplog.records if "body:" in r.getMessage()]
    assert len(body_logs) == 1 and "noRecordsMatch" in body_logs[0]
    # only the first 64 KB of the body reach the log
    assert body_logs[0].count("z") == 64 * 1024 - len(b"<error>noRecordsMatch</error>")


def test_5xx_backoff_then_success() -> None:
    clock = FakeClock()
    t = ScriptedTransport(response(503), response(502), response(200, b"ok"))
    http = make_http(t, clock, policy=HostPolicy(default_interval_s=0.0))
    assert http.get(URL) is not None
    assert [s for s in clock.sleeps if s >= 5] == [5.0, 10.0]


def test_5xx_all_tries_fail() -> None:
    clock = FakeClock()
    t = ScriptedTransport(response(500), response(500), response(500))
    http = make_http(t, clock, policy=HostPolicy(default_interval_s=0.0))
    assert http.get(URL) is None
    assert http.errors[-1].message == "api.example.org: 3 tries failed"


def test_transport_error_is_retried() -> None:
    clock = FakeClock()
    t = ScriptedTransport(TransportError("timeout"), response(200, b"ok"))
    http = make_http(t, clock, policy=HostPolicy(default_interval_s=0.0))
    assert http.get(URL) is not None
    assert 5.0 in clock.sleeps


def test_secret_headers_never_in_url_cache_key_or_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "Zq" + "9" * 30  # built at runtime, never a literal token in the tree
    clock = FakeClock()
    t = ScriptedTransport(response(200, b"ok"), response(404, b"missing"))
    cache = Cache(tmp_path / "cache", clock=clock.time)
    http = make_http(t, clock, cache=cache)
    log_file = tmp_path / "kallimachos.log"
    handler = logging.FileHandler(log_file, encoding="utf-8")
    logger = logging.getLogger("kallimachos.http")
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        with caplog.at_level(logging.DEBUG, logger="kallimachos.http"):
            http.get(URL, headers={"Authorization": f"Bearer {secret}", "x-api-key": secret})
            http.get(URL + "/missing", params={"api_key": secret, "email": "someone@example.org"})
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(old_level)
    sent_url, sent_headers, _ = t.calls[0]
    assert secret not in sent_url
    assert sent_headers["Authorization"].endswith(secret)  # sent, but only as a header
    assert secret not in log_file.read_text(encoding="utf-8")
    assert "someone@example.org" not in log_file.read_text(encoding="utf-8")
    assert secret not in caplog.text
    for err in http.errors:
        assert secret not in err.message and secret not in (err.url or "")
    for entry in (tmp_path / "cache").glob("*.json"):
        assert secret not in entry.read_text(encoding="utf-8")
    assert cache_key(URL, {"api_key": secret, "q": "a"}) == cache_key(URL, {"q": "a"})


def test_cache_key_is_canonical() -> None:
    assert cache_key(URL, {"b": "2", "a": "1"}) == cache_key(URL + "?a=1", {"b": "2"})
    assert cache_key(URL, {"a": "1"}) != cache_key(URL, {"a": "2"})
    assert cache_key("HTTPS://API.example.org/works", None) == cache_key(URL, None)
    assert len(cache_key(URL, None)) == 40


def test_redact_url() -> None:
    red = redact_url(URL + "?api_key=abc&mailto=x%40example.org&q=1")
    assert "abc" not in red and "example.org&" not in red
    assert "q=1" in red and "api_key=***" in red


def test_cache_serves_second_request(tmp_path: Path) -> None:
    clock = FakeClock()
    t = ScriptedTransport(response(200, b"first"))
    http = make_http(t, clock, cache=Cache(tmp_path, clock=clock.time))
    assert http.get(URL, kind="single") is not None
    again = http.get(URL, kind="single")
    assert again is not None and again.from_cache and again.body == b"first"
    assert len(t.calls) == 1


def test_pdf_kind_is_never_cached(tmp_path: Path) -> None:
    clock = FakeClock()
    t = ScriptedTransport(response(200, b"%PDF"), response(200, b"%PDF"))
    http = make_http(t, clock, cache=Cache(tmp_path, clock=clock.time))
    http.get(URL + "/a.pdf", kind="pdf")
    http.get(URL + "/a.pdf", kind="pdf")
    assert len(t.calls) == 2
    assert list(tmp_path.glob("*.json")) == []


def test_offline_serves_only_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    cache = Cache(tmp_path, clock=clock.time)
    online = make_http(ScriptedTransport(response(200, b"cached")), clock, cache=cache)
    online.get(URL, kind="list")
    monkeypatch.setenv("OFFLINE", "1")
    offline = Http(HostPolicy(), cache, None, user_agent())
    assert offline.offline
    hit = offline.get(URL, kind="list")
    assert hit is not None and hit.body == b"cached"
    assert offline.get(URL + "/other") is None
    assert "offline" in offline.errors[-1].message


def test_offline_flag_without_cache_never_touches_network() -> None:
    http = Http(HostPolicy(), None, None, user_agent(), offline=True)
    assert http.get("https://api.example.org/anything") is None


def test_crossref_user_agent_mailto_only_when_given() -> None:
    assert "mailto:" not in crossref_user_agent(None)
    assert "mailto:" not in crossref_user_agent("  ")
    ua = crossref_user_agent("team@example.org", version="9.9")
    assert (
        ua == "kallimachos/9.9 (+https://github.com/oselcukk/kallimachos; mailto:team@example.org)"
    )
    assert "mailto:" not in user_agent()


def test_info_log_never_shows_query_terms(caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    t = ScriptedTransport(response(200, b"ok"), response(404, b"nope"))
    http = make_http(t, clock)
    with caplog.at_level(logging.INFO, logger="kallimachos.http"):
        http.get(URL, params={"search": "Example Author Name"})
        http.get(URL, params={"search": "Fictional Title Words"})
    assert "Example" not in caplog.text and "Fictional" not in caplog.text
    assert "api.example.org/works" in caplog.text


def test_response_url_is_redacted_before_it_leaves_the_client(tmp_path: Path) -> None:
    """``Http.get`` hands back a response whose URL carries ``api_key=***``; the cached entry
    (written by the same call) holds the same redacted URL."""
    from typing import Any

    from kallimachos.http.client import HostPolicy, Http
    from kallimachos.interfaces import HttpResponse

    def transport(url: str, headers: Any, timeout: float, *rest: Any) -> HttpResponse:
        return HttpResponse(url=url, status=200, headers={}, body=b"{}")

    http = Http(
        HostPolicy(), Cache(tmp_path), None, "ua", transport=transport, sleep=lambda _s: None
    )
    resp = http.get(
        "https://api.example.org/w", params={"api_key": "SENTINELKEY", "email": "s@example.org"}
    )
    assert resp is not None and "SENTINELKEY" not in resp.url and "s%40example" not in resp.url
    assert "api_key=***" in resp.url
    on_disk = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*.json"))
    assert on_disk and "SENTINELKEY" not in on_disk and "example.org" in on_disk


def test_fixture_transport_from_env_replays_recorded_exchanges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kallimachos.http.client import fixture_transport_from_env

    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    monkeypatch.setenv("KALLIMACHOS_HTTP_FIXTURES", str(fixtures / "arxiv"))
    transport = fixture_transport_from_env()
    assert transport is not None
    resp = transport("https://export.arxiv.org/api/query?search_query=x&api_key=K", {}, 10.0)
    assert resp.status == 200 and resp.body.lstrip().startswith(b"<")
    monkeypatch.setenv("KALLIMACHOS_HTTP_FIXTURES", str(tmp_path / "nowhere"))
    assert fixture_transport_from_env() is None
    monkeypatch.delenv("KALLIMACHOS_HTTP_FIXTURES")
    assert fixture_transport_from_env() is None
