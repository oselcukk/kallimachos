"""bioRxiv/medRxiv source.

api.biorxiv.org details/{server}/{from}/{to}/{cursor}/json, 30 per call.
Fixtures: ``tests/fixtures/biorxiv/``.

Paging: ``cursor`` starts at 0 and grows by the number of records returned (30 per call) until
``messages[0].total`` is reached or a short page arrives. A DOI may appear once per version; the
newest version wins.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Iterator, Mapping
from typing import Any

from kallimachos.config.schema import BiorxivConfig
from kallimachos.interfaces import (
    Author,
    HttpClient,
    Ident,
    IdentKind,
    OaCandidate,
    Record,
    SourceTest,
    Window,
    WorkKind,
)
from kallimachos.profile.schema import Profile
from kallimachos.sources.base import (
    BaseSource,
    Fetch,
    SecretResolver,
    clean_text,
    normalize_doi,
    parse_iso_date,
    parse_json,
)

API = "https://api.biorxiv.org/details"
PAGE = 30
MAX_PAGES = 200
SITES = {"biorxiv": "https://www.biorxiv.org", "medrxiv": "https://www.medrxiv.org"}
LABELS = {"biorxiv": "bioRxiv", "medrxiv": "medRxiv"}
VERSIONED_URL_RE = re.compile(r"^https://www\.(?:bio|med)rxiv\.org/content/.+?(v\d+)$")


def bx_name(raw: str) -> str:
    """``"Author, E. A."`` -> ``"E. A. Author"``."""
    raw = clean_text(raw)
    if "," in raw:
        last, first = raw.split(",", 1)
        return clean_text(f"{first} {last}")
    return raw


def _version(item: Mapping[str, Any]) -> int:
    try:
        return int(str(item.get("version") or "1"))
    except ValueError:
        return 1


def parse_item(item: Mapping[str, Any], server: str) -> Record | None:
    title = clean_text(item.get("title"))
    doi = normalize_doi(item.get("doi"))
    if not title or not doi:
        return None
    version = _version(item)
    abstract = clean_text(item.get("abstract")) or None
    published_doi = normalize_doi(item.get("published")) if item.get("published") != "NA" else None
    site = SITES.get(server, SITES["biorxiv"])
    return Record(
        title=title,
        authors=[
            Author(name=bx_name(a)) for a in str(item.get("authors") or "").split(";") if a.strip()
        ],
        abstract=abstract,
        abstract_source=server if abstract else None,
        venue=LABELS.get(server, server),
        published=parse_iso_date(item.get("date")),
        created=parse_iso_date(item.get("date")),
        doi=doi,
        url=f"{site}/content/{doi}v{version}",
        categories=[clean_text(item.get("category"))] if item.get("category") else [],
        keywords=[f"published:{published_doi}"] if published_doi else [],
        kind=WorkKind.PREPRINT,
        sources={"biorxiv"},
    )


class BiorxivSource(BaseSource):
    name = "biorxiv"
    needs_key = False
    default_interval_s = 1.0
    hosts = ("api.biorxiv.org", "www.biorxiv.org", "www.medrxiv.org")

    def __init__(self, config: object, *, resolve_secret: SecretResolver | None = None) -> None:
        """``config`` is ``Config.sources.biorxiv``."""
        super().__init__(config, resolve_secret=resolve_secret)
        self.config = config if isinstance(config, BiorxivConfig) else BiorxivConfig()

    def discover(self, window: Window, profile: Profile, http: HttpClient) -> Iterator[Record]:
        start = window.start.astimezone(_dt.UTC).date().isoformat()
        last = (window.end - _dt.timedelta(microseconds=1)).astimezone(_dt.UTC).date().isoformat()
        for server in self.config.servers:
            newest: dict[str, tuple[int, Record]] = {}
            for item in self._pages(http, f"{API}/{server}/{start}/{last}"):
                rec = parse_item(item, server)
                if rec is None or rec.doi is None:
                    continue
                version = _version(item)
                if rec.doi not in newest or newest[rec.doi][0] < version:
                    rec.queries = {f"biorxiv:{server}"}
                    newest[rec.doi] = (version, rec)
            for _v, rec in newest.values():
                yield rec

    def _pages(self, http: HttpClient, base: str) -> Iterator[Mapping[str, Any]]:
        cursor = 0
        for _page in range(MAX_PAGES):
            result = self.fetch(http, f"{base}/{cursor}/json")
            if not result.ok:
                return
            data = parse_json(result.body)
            if not isinstance(data, dict):
                self.error("biorxiv: unexpected response shape")
                return
            items = data.get("collection") or []
            yield from items
            messages = data.get("messages") or [{}]
            try:
                total = int(str(messages[0].get("total") or 0))
            except (ValueError, AttributeError):
                total = 0
            cursor += len(items)
            if len(items) < PAGE or cursor >= total:
                return

    def _versions(self, http: HttpClient, doi: str) -> tuple[str, Mapping[str, Any]] | None:
        for server in self.config.servers:
            result = self.fetch(http, f"{API}/{server}/{doi}/na/json", kind="single")
            if not result.ok:
                continue
            data = parse_json(result.body)
            items = data.get("collection") if isinstance(data, dict) else None
            if isinstance(items, list) and items:
                return server, max(items, key=_version)
        return None

    def metadata(self, ident: Ident, http: HttpClient) -> Record | None:
        if ident.kind is not IdentKind.DOI:
            return None
        found = self._versions(http, normalize_doi(ident.value) or ident.value)
        return parse_item(found[1], found[0]) if found else None

    def oa_pdf_candidates(self, record: Record, http: HttpClient) -> list[OaCandidate]:
        if not record.doi:
            return []
        url = record.url or ""
        m = VERSIONED_URL_RE.match(url)
        if m is None:
            found = self._versions(http, record.doi)
            rec = parse_item(found[1], found[0]) if found else None
            m = VERSIONED_URL_RE.match(rec.url or "") if rec else None
        if m is None:
            return []
        return [OaCandidate(url=f"{m.group(0)}.full.pdf", source="biorxiv", version=m.group(1))]

    def test(self, http: HttpClient) -> SourceTest:
        def check(result: Fetch) -> str:
            data = parse_json(result.body)
            return "" if isinstance(data, dict) and "messages" in data else "unexpected response"

        server = (self.config.servers or ["biorxiv"])[0]
        return self.timed_test(lambda: self.fetch(http, f"{API}/{server}/1d/0/json"), check)
