"""http.cache: TTL per kind, purge, corrupt entries."""

from __future__ import annotations

from pathlib import Path

import pytest

from kallimachos.http.cache import Cache
from kallimachos.http.client import cache_key
from tests.unit.fakes import FakeClock, response


def test_ttl_per_kind(tmp_path: Path) -> None:
    clock = FakeClock()
    cache = Cache(tmp_path, clock=clock.time)
    k1, k2 = cache_key("https://a.example.org/1", None), cache_key("https://a.example.org/2", None)
    cache.put(k1, "single", response(200, b"meta", headers={"Set-Cookie": "s=1", "ETag": "e"}))
    cache.put(k2, "list", response(200, b"list"))
    clock.advance(2 * 86400)
    single = cache.get(k1, "single")
    assert single is not None and single.body == b"meta" and single.from_cache
    assert "Set-Cookie" not in single.headers and single.headers["ETag"] == "e"
    assert cache.get(k2, "list") is None
    assert cache.purge_expired() == 1
    clock.advance(30 * 86400)
    assert cache.purge_expired() == 1
    assert list(tmp_path.glob("*.json")) == []


def test_corrupt_entry_is_a_miss(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    key = cache_key("https://a.example.org/x", None)
    (tmp_path / f"{key}.json").write_text("{not json", encoding="utf-8")
    assert cache.get(key, "list") is None
    assert cache.purge_expired() == 1


def test_rejects_non_sha1_keys(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Cache(tmp_path).get("../../etc/passwd", "list")


def test_cached_url_never_keeps_secret_query_values(tmp_path: Path) -> None:
    """The transport reports the final URL with its query; the entry stores it redacted."""
    cache = Cache(tmp_path)
    url = "https://api.example.org/works?q=x&api_key=SENTINELKEY&email=sentinel%40example.org"
    key = cache_key(url, None)
    cache.put(key, "single", response(200, b"{}", url=url))
    raw = (tmp_path / f"{key}.json").read_text(encoding="utf-8")
    assert "SENTINELKEY" not in raw and "sentinel" not in raw
    assert "api_key=***" in raw and "email=***" in raw and "q=x" in raw
    hit = cache.get(key, "single")
    assert hit is not None and "SENTINELKEY" not in hit.url
    assert cache.entries_with_secrets() == []


def test_entries_with_secrets_finds_old_entries(tmp_path: Path) -> None:
    import json

    cache = Cache(tmp_path)
    key = cache_key("https://api.example.org/old", None)
    (tmp_path / f"{key}.json").write_text(
        json.dumps(
            {
                "stored_at": 1.0,
                "kind": "single",
                "url": "https://api.example.org/old?api_key=OLDKEY",
                "status": 200,
                "headers": {},
                "body_b64": "",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "clean.json").write_text(
        json.dumps({"url": "https://api.example.org/x?mailto=***"}), encoding="utf-8"
    )
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    assert cache.entries_with_secrets() == [tmp_path / f"{key}.json"]
