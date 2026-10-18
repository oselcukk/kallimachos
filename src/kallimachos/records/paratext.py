"""Paratext and repository-venue pre-filter."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

from kallimachos.interfaces import Record
from kallimachos.records.model import normalize_doi
from kallimachos.records.normalize import match_key

PARATEXT_PATTERNS: tuple[str, ...] = (
    r"\beditorial board\b",
    r"\berratum\b",
    r"\bcorrigendum\b",
    r"\bpreface\b",
    r"\bobituary\b",
    r"\bfront matter\b",
)

REPOSITORY_VENUES: tuple[str, ...] = ("zenodo", "figshare", "ssrn", "researchgate")

REPOSITORY_DOI_PREFIXES: dict[str, str] = {
    "10.5281/": "zenodo",
    "10.6084/": "figshare",
    "10.2139/": "ssrn",
}
"""DOI prefixes registered by the generic repositories above."""


def _compile(patterns: Sequence[str]) -> list[re.Pattern[str]]:
    out: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            out.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            # a user-supplied plain phrase that is not a valid regex is matched literally
            out.append(re.compile(r"\b" + re.escape(pattern) + r"\b", re.IGNORECASE))
    return out


def classify(
    record: Record,
    *,
    extra_paratext: Sequence[str] = (),
    extra_repositories: Sequence[str] = (),
) -> Literal["paratext", "repository"] | None:
    """``paratext`` for editorial matter or title == venue; ``repository`` for generic repositories.

    Titles are matched on their accent-free, lower-case form, so ``Erratum:`` and ``ERRATUM``
    both count. ``extra_paratext`` (``scoring.paratext_extra``) may hold regular expressions or
    plain phrases; ``extra_repositories`` (``scoring.repository_venues_extra``) are venue names.
    """
    title = (record.title or "").strip()
    title_key = match_key(title)
    for rx in _compile([*PARATEXT_PATTERNS, *extra_paratext]):
        if rx.search(title) or rx.search(title_key):
            return "paratext"
    venue_key = match_key(record.venue or "")
    if title_key and venue_key and title_key == venue_key:
        return "paratext"

    repositories = [match_key(r) for r in (*REPOSITORY_VENUES, *extra_repositories) if r.strip()]
    if venue_key:
        tokens = f" {venue_key} "
        if any(f" {repo} " in tokens for repo in repositories if repo):
            return "repository"
    doi = normalize_doi(record.doi)
    if doi and any(doi.startswith(prefix) for prefix in REPOSITORY_DOI_PREFIXES):
        return "repository"
    return None
