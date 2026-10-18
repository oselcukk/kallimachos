"""Secret references.

Config holds only ``keyring:<service>`` or ``env:<VAR>`` references; the value is resolved at use.

``keyring:<name>`` reads the OS keychain entry ``(service="kallimachos", username=<name>)``;
``keyring:<service>/<name>`` names the service explicitly (``keyring:kallimachos/openalex``).
"""

from __future__ import annotations

import os
import re
from typing import Literal

from kallimachos.interfaces import ConfigError

TOKEN_LIKE_RE = re.compile(r"^(sk-|ghp_|bot[0-9]+:)|^[A-Za-z0-9_-]{20,}$")
"""The basic token shape (``config validate`` rejects such values with exit 2); the full test is
:func:`looks_like_secret`."""

REF_RE = re.compile(r"^(keyring|env):(.+)$")

KEYRING_SERVICE = "kallimachos"

_PREFIX_RE = re.compile(
    r"^(sk-|ghp_|gho_|ghu_|ghs_|github_pat_|glpat-|xox[abposr]-|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30}|"
    r"bot[0-9]+:|[0-9]{6,}:[A-Za-z0-9_-]{30,}$|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.)"
)
_GENERIC_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]{32,}={0,2}$")
_WORD_RE = re.compile(r"^[A-Za-z]*$")
_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_SECRET_QUERY = frozenset(
    {"api_key", "apikey", "key", "token", "access_token", "password", "secret", "client_secret"}
)


def ref_kind(ref: str) -> Literal["keyring", "env"] | None:
    m = REF_RE.match(ref)
    if not m:
        return None
    return "keyring" if m.group(1) == "keyring" else "env"


def looks_like_secret(value: str) -> bool:
    """True for token-like strings (>=20 chars of ``[A-Za-z0-9_-]`` or well-known prefixes).

    A long string made only of lower-case dictionary-like words joined by ``-``/``_``
    (``analytic-number-theory-notes``) is not treated as a token: real tokens carry digits or
    mixed case. Well-known prefixes (``sk-``, ``ghp_``, ``bot<digits>:``) always count.
    """
    text = value.strip()
    if _PREFIX_RE.match(text):
        return True
    if _URL_RE.match(text):
        return _url_carries_secret(text)
    if _BASE64_RE.match(text) and ("/" in text or "+" in text or text.endswith("=")):
        return True  # base64 material (AWS secret keys, raw key bytes)
    if not _GENERIC_RE.match(text):
        return False
    segments = [s for s in re.split(r"[-_]", text) if s]
    plain_words = all(_WORD_RE.match(s) and (s.islower() or s.istitle()) for s in segments)
    return not (len(segments) > 1 and plain_words)


def _url_carries_secret(url: str) -> bool:
    """A URL is a secret when its query sets a key-like parameter (``?key=...``,
    ``&token=...``); plain endpoint URLs are configuration, not secrets."""
    from urllib.parse import parse_qsl, urlsplit

    query = urlsplit(url).query
    return any(
        k.lower() in _SECRET_QUERY and v for k, v in parse_qsl(query, keep_blank_values=True)
    )


def _split_keyring(name: str) -> tuple[str, str]:
    service, sep, user = name.partition("/")
    if sep and service and user:
        return service, user
    return KEYRING_SERVICE, name


def resolve_ref(ref: str) -> str:
    """Return the secret for ``keyring:<service>`` (``keyring`` package, service ``kallimachos``) or
    ``env:<VAR>``. Raises :class:`kallimachos.interfaces.ConfigError` when missing.

    Error messages name the reference, never the value.
    """
    m = REF_RE.match(ref or "")
    if not m:
        raise ConfigError(f"not a secret reference: expected keyring:<name> or env:<VAR> ({ref!r})")
    kind, name = m.group(1), m.group(2).strip()
    if kind == "env":
        value = os.environ.get(name)
        if not value:
            raise ConfigError(f"secret {ref} is not set in the environment")
        return value
    service, user = _split_keyring(name)
    try:
        import keyring

        secret = keyring.get_password(service, user)
    except Exception as exc:  # any backend failure means "not available", never a crash
        raise ConfigError(f"keyring backend unavailable for {ref}: {type(exc).__name__}") from None
    if not secret:
        raise ConfigError(f"secret {ref} not found in the OS keyring")
    return secret


def is_present(ref: str | None) -> bool:
    """Whether the referenced secret exists, without returning it (``doctor`` reports
    present/absent)."""
    if not ref:
        return False
    try:
        resolve_ref(ref)
    except ConfigError:
        return False
    return True
