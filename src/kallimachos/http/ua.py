"""User-Agent strings."""

from __future__ import annotations

from kallimachos.version import __version__

REPO_URL = "https://github.com/oselcukk/kallimachos"


def user_agent(version: str = __version__) -> str:
    """``kallimachos/<ver> (+https://github.com/oselcukk/kallimachos)``."""
    return f"kallimachos/{version} (+{REPO_URL})"


def crossref_user_agent(mailto: str | None, version: str = __version__) -> str:
    """Crossref polite pool: the mailto goes only to Crossref and only when configured.

    ``kallimachos/<ver> (+https://github.com/oselcukk/kallimachos; mailto:<address>)``. Without an
    address (or with a blank/obviously malformed one) the plain :func:`user_agent` is returned.
    """
    address = (mailto or "").strip()
    if not address or "@" not in address or any(c in address for c in "()\r\n;"):
        return user_agent(version)
    return f"kallimachos/{version} (+{REPO_URL}; mailto:{address})"
