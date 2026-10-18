"""secrets: token detection and reference resolution."""

from __future__ import annotations

import pytest

from kallimachos import secrets
from kallimachos.interfaces import ConfigError

TOKEN = "Ab3" + "x9" * 12  # 27 chars, built at runtime


@pytest.mark.parametrize(
    "value",
    [
        "sk-" + "a1" * 12,
        "ghp_" + "B2" * 12,
        "bot123456:" + "C3" * 12,
        TOKEN,
        "a" * 19 + "1",
    ],
)
def test_looks_like_secret_true(value: str) -> None:
    assert secrets.looks_like_secret(value)


@pytest.mark.parametrize(
    "value",
    [
        "short",
        "keyring:kallimachos/openalex",
        "env:OPENALEX_API_KEY",
        "https://export.arxiv.org/api/query",
        "analytic-number-theory-notes",
        "scholaralerts-noreply@google.com",
        "{authors}_{year}_{short_title}",
        "Europe/Example_City",
    ],
)
def test_looks_like_secret_false(value: str) -> None:
    assert not secrets.looks_like_secret(value)


def test_ref_kind() -> None:
    assert secrets.ref_kind("env:X") == "env"
    assert secrets.ref_kind("keyring:x") == "keyring"
    assert secrets.ref_kind("plain") is None


def test_env_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KALLI_TEST_SECRET", TOKEN)
    assert secrets.resolve_ref("env:KALLI_TEST_SECRET") == TOKEN
    assert secrets.is_present("env:KALLI_TEST_SECRET")
    monkeypatch.delenv("KALLI_TEST_SECRET")
    with pytest.raises(ConfigError) as exc:
        secrets.resolve_ref("env:KALLI_TEST_SECRET")
    assert TOKEN not in str(exc.value)
    assert not secrets.is_present("env:KALLI_TEST_SECRET")
    assert not secrets.is_present(None)
    assert not secrets.is_present("")


def test_keyring_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring

    store = {("kallimachos", "openalex"): TOKEN, ("other", "name"): "v"}
    calls: list[tuple[str, str]] = []

    def fake_get(service: str, user: str) -> str | None:
        calls.append((service, user))
        return store.get((service, user))

    monkeypatch.setattr(keyring, "get_password", fake_get)
    assert secrets.resolve_ref("keyring:openalex") == TOKEN
    assert secrets.resolve_ref("keyring:kallimachos/openalex") == TOKEN
    assert secrets.resolve_ref("keyring:other/name") == "v"
    assert not secrets.is_present("keyring:missing")
    assert calls[0] == ("kallimachos", "openalex")


def test_keyring_backend_failure_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring

    def broken(service: str, user: str) -> str | None:
        raise RuntimeError("no backend")

    monkeypatch.setattr(keyring, "get_password", broken)
    with pytest.raises(ConfigError):
        secrets.resolve_ref("keyring:openalex")
    assert not secrets.is_present("keyring:openalex")


def test_not_a_reference() -> None:
    with pytest.raises(ConfigError):
        secrets.resolve_ref(TOKEN)


@pytest.mark.parametrize(
    "value",
    [
        "123456789:AAHfakeFAKEfake1234567890abcdefghi",  # Telegram token without bot prefix
        "xoxb-123-456-abc",
        "github_pat_11ABCDEFG0123456789_abcdefghijklmnop",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc123def456",  # JWT
        "AKIA" + "EXAMPLEKEY123456",
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",  # base64-like with /
        "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXo=",
        "https://example.org/feed?key=AbC1dEf2GhI3jKl4MnO5pQr6",
        "https://example.org/rss?user=u&token=T0ken",
    ],
)
def test_looks_like_secret_extended_shapes(value: str) -> None:
    assert secrets.looks_like_secret(value)


@pytest.mark.parametrize(
    "value",
    [
        "https://example.org/feed?category=math&page=2",
        "https://export.arxiv.org/api/query?search_query=cat:math.NT",
        "0123456789abcdef012",  # 19 hex chars: too short for the generic shape
        "large-sieve-notes-and-drafts",  # dictionary words joined by dashes
    ],
)
def test_looks_like_secret_still_false(value: str) -> None:
    assert not secrets.looks_like_secret(value)
