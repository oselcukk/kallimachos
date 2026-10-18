"""arXiv source.

http://export.arxiv.org/api/query: cat: OR queries, submittedDate window in GMT,
start/max_results paging, alias map math.MP->math-ph, math.IT->cs.IT; https first, http fallback.
Fixtures: ``tests/fixtures/arxiv/``.

Paging limits from the API manual: at most 2000 results per call and ``start + max_results``
never above 30000 (the API answers HTTP 400 beyond that).
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterator
from xml.etree import ElementTree as ET

from kallimachos.config.schema import ArxivConfig
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
    ARXIV_DOI_PREFIX,
    BaseSource,
    Fetch,
    SecretResolver,
    chunked,
    clean_text,
    element_text,
    find_arxiv,
    normalize_doi,
    parse_datetime,
    parse_xml,
    strip_version,
)

NS = {
    "a": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "os": "http://a9.com/-/spec/opensearch/1.1/",
}
CATEGORY_ALIASES: dict[str, str] = {"math.MP": "math-ph", "math.IT": "cs.IT"}
"""arXiv cross-list aliases: the API indexes the canonical name only."""

MAX_PAGE = 2000
MAX_OFFSET = 30000
CATEGORIES_PER_QUERY = 20
TEST_CATEGORY = "math.NT"


def canonical_category(cat: str) -> str:
    return CATEGORY_ALIASES.get(cat, cat)


def gmt_stamp(moment: _dt.datetime) -> str:
    return moment.astimezone(_dt.UTC).strftime("%Y%m%d%H%M")


def parse_entry(entry: ET.Element) -> Record | None:
    """One Atom ``<entry>`` of the API feed -> Record (``url`` keeps the version)."""
    raw_id = (entry.findtext("a:id", "", NS) or "").strip()
    if "/abs/" not in raw_id:
        return None
    arxiv_id, version = strip_version(raw_id.rsplit("/abs/", 1)[1])
    title = element_text(entry.find("a:title", NS))
    if not title:
        return None
    published = parse_datetime(entry.findtext("a:published", None, NS))
    updated = parse_datetime(entry.findtext("a:updated", None, NS))
    primary = entry.find("arxiv:primary_category", NS)
    cats = [c.get("term", "") for c in entry.findall("a:category", NS) if c.get("term")]
    doi = normalize_doi(entry.findtext("arxiv:doi", None, NS))
    jref = clean_text(entry.findtext("arxiv:journal_ref", None, NS)) or None
    abstract = element_text(entry.find("a:summary", NS)) or None
    stamp = updated or published
    return Record(
        title=title,
        authors=[
            Author(name=clean_text(a.findtext("a:name", "", NS)))
            for a in entry.findall("a:author", NS)
            if clean_text(a.findtext("a:name", "", NS))
        ],
        abstract=abstract,
        abstract_source="arxiv" if abstract else None,
        venue=jref,
        published=published.date() if published else None,
        created=stamp.date() if stamp else None,
        doi=doi,
        arxiv_id=arxiv_id,
        url=f"https://arxiv.org/abs/{arxiv_id}{version or ''}",
        categories=cats,
        primary_category=primary.get("term")
        if primary is not None
        else (cats[0] if cats else None),
        kind=WorkKind.PREPRINT,
        sources={"arxiv"},
    )


class ArxivSource(BaseSource):
    name = "arxiv"
    needs_key = False
    default_interval_s = 3.1
    hosts = ("export.arxiv.org", "arxiv.org")

    def __init__(self, config: object, *, resolve_secret: SecretResolver | None = None) -> None:
        """``config`` is ``Config.sources.arxiv``."""
        super().__init__(config, resolve_secret=resolve_secret)
        self.config = config if isinstance(config, ArxivConfig) else ArxivConfig()
        self._base = self.config.base_url

    # -- HTTP with https -> http fallback ------------------------------------------------------

    def _query(self, http: HttpClient, params: dict[str, str], *, kind: str = "list") -> Fetch:
        result = self.fetch(http, self._base, params=params, kind=kind, timeout=180.0)
        if result.ok or not self._base.startswith("https://"):
            return result
        status = result.status
        if status is not None and 400 <= status < 500 and status != 403:
            return result  # a client error (e.g. offset beyond 30000) is not a transport problem
        fallback = "http://" + self._base[len("https://") :]
        retry = self.fetch(http, fallback, params=params, kind=kind, timeout=180.0)
        if retry.ok:
            self.warn(f"arxiv: {self._base} failed, using {fallback} for this run")
            self._base = fallback
        return retry

    # -- plug-in API ---------------------------------------------------------------------------

    def categories(self, profile: Profile) -> list[str]:
        cats: list[str] = []
        for ch in profile.channels:
            for cat in [*ch.arxiv.primary, *ch.arxiv.secondary]:
                c = canonical_category(cat.strip())
                if c and c not in cats:
                    cats.append(c)
        return cats

    def discover(self, window: Window, profile: Profile, http: HttpClient) -> Iterator[Record]:
        cats = self.categories(profile)
        if not cats:
            self.warn("arxiv: the profile names no arXiv categories; nothing to query")
            return
        seen: set[str] = set()
        date_q = f"submittedDate:[{gmt_stamp(window.start)} TO {gmt_stamp(window.end)}]"
        for group in chunked(cats, CATEGORIES_PER_QUERY):
            cat_q = " OR ".join(f"cat:{c}" for c in group)
            query = f"({cat_q}) AND {date_q}"
            for rec in self._pages(http, query):
                if rec.arxiv_id and rec.arxiv_id not in seen:
                    seen.add(rec.arxiv_id)
                    rec.queries = {f"arxiv:{c}" for c in group if c in rec.categories} or {
                        "arxiv:categories"
                    }
                    yield rec

    def _pages(self, http: HttpClient, query: str) -> Iterator[Record]:
        page_size = max(1, min(self.config.page_size, MAX_PAGE))
        limit = min(self.config.max_records, MAX_OFFSET)
        start = 0
        total: int | None = None
        empty_retries = 0
        while start < limit:
            size = min(page_size, limit - start, MAX_OFFSET - start)
            if size <= 0:
                break
            params = {
                "search_query": query,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
                "start": str(start),
                "max_results": str(size),
            }
            result = self._query(http, params)
            if not result.ok:
                return
            root = parse_xml(result.body)
            if root is None:
                self.error("arxiv: response is not valid Atom XML")
                return
            if total is None:
                total_text = root.findtext("os:totalResults", "0", NS) or "0"
                total = int(total_text) if total_text.strip().isdigit() else 0
            entries = root.findall("a:entry", NS)
            if not entries:
                # the API sometimes returns an empty page in the middle of a result set
                if start < total and empty_retries < 1:
                    empty_retries += 1
                    continue
                return
            for entry in entries:
                rec = parse_entry(entry)
                if rec is not None:
                    yield rec
            start += len(entries)
            if start >= total:
                return

    def _by_id(self, http: HttpClient, arxiv_id: str) -> tuple[Record, str | None] | None:
        result = self._query(http, {"id_list": arxiv_id, "max_results": "1"}, kind="single")
        if not result.ok:
            return None
        root = parse_xml(result.body)
        if root is None:
            return None
        for entry in root.findall("a:entry", NS):
            raw = (entry.findtext("a:id", "", NS) or "").rsplit("/abs/", 1)[-1]
            rec = parse_entry(entry)
            if rec is not None:
                return rec, strip_version(raw)[1]
        return None

    def metadata(self, ident: Ident, http: HttpClient) -> Record | None:
        if ident.kind is IdentKind.ARXIV:
            arxiv_id = strip_version(ident.value)[0]
        elif ident.kind is IdentKind.DOI and ident.value.lower().startswith(ARXIV_DOI_PREFIX):
            arxiv_id = ident.value.lower()[len(ARXIV_DOI_PREFIX) :]
        else:
            return None
        found = self._by_id(http, arxiv_id)
        return found[0] if found else None

    def oa_pdf_candidates(self, record: Record, http: HttpClient) -> list[OaCandidate]:
        arxiv_id = record.arxiv_id
        version: str | None = None
        if arxiv_id:
            arxiv_id, version = strip_version(arxiv_id)
        else:
            hit = find_arxiv(record.url, record.doi)
            if hit is None:
                return []
            arxiv_id, version = hit
        if version is None:
            hit = find_arxiv(record.url)
            if hit and hit[0] == arxiv_id:
                version = hit[1]
        if version is None:
            found = self._by_id(http, arxiv_id)
            version = found[1] if found else None
        url = f"https://arxiv.org/pdf/{arxiv_id}{version or ''}"
        return [OaCandidate(url=url, source="arxiv", version=version)]

    def test(self, http: HttpClient) -> SourceTest:
        def check(result: Fetch) -> str:
            root = parse_xml(result.body)
            if root is None or root.tag != f"{{{NS['a']}}}feed":
                return "response is not an arXiv Atom feed"
            return ""

        params = {"search_query": f"cat:{TEST_CATEGORY}", "start": "0", "max_results": "1"}
        return self.timed_test(lambda: self._query(http, params), check)


__all__ = ["CATEGORY_ALIASES", "ArxivSource", "canonical_category", "parse_entry"]
