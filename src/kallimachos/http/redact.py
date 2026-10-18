"""Secret-bearing query parameters and URL redaction, shared by the HTTP client, the cache and
``doctor``. Kept free of other imports so the cache can use it without importing the client."""

from __future__ import annotations

import urllib.parse

SECRET_PARAMS = frozenset(
    {
        "api_key",
        "apikey",
        "key",
        "token",
        "access_token",
        "password",
        "secret",
        "client_secret",
        "email",
        "mailto",
    }
)
"""Query parameters that never enter cache keys, cache entries or logs (redacted as ``***``)."""

REDACTED = "***"


def is_secret_param(name: str) -> bool:
    return name.lower() in SECRET_PARAMS


def redact_url(url: str) -> str:
    """URL safe for logs, error records and cache entries: secret query values become ``***``."""
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    query = [
        (k, REDACTED if is_secret_param(k) else v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    ]
    redacted = urllib.parse.urlencode(query, safe="*")
    return urllib.parse.urlunsplit(parts._replace(query=redacted))


def carries_secret(url: str) -> bool:
    """True when the URL still holds an unredacted value for a secret parameter."""
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return False
    return any(
        is_secret_param(k) and v not in ("", REDACTED)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    )
