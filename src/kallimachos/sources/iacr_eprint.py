"""IACR ePrint source.

RSS 2.0 at eprint.iacr.org/rss/rss.xml, citation_* meta on paper pages, PDF at
eprint.iacr.org/<id>.pdf.
Fixtures: ``tests/fixtures/iacr_eprint/``.

``?order=recent`` lists papers by publication date (eprint.iacr.org/rss). Each author is one
``dc:creator`` element in the standard format.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from html.parser import HTMLParser

from kallimachos.config.schema import IacrEprintConfig
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
    element_text,
    parse_datetime,
    parse_iso_date,
    parse_xml,
)

RSS_URL = "https://eprint.iacr.org/rss/rss.xml"
PAGE_URL = "https://eprint.iacr.org/{id}"
PDF_URL = "https://eprint.iacr.org/{id}.pdf"
DC = "{http://purl.org/dc/elements/1.1/}"
EPRINT_ID_RE = re.compile(r"(\d{4}/\d{3,})")


class _MetaParser(HTMLParser):
    """Collects ``<meta name=... content=...>`` and ``<meta property=...>`` pairs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        a = {k: v or "" for k, v in attrs}
        key = a.get("name") or a.get("property")
        if key:
            self.meta.append((key, a.get("content", "")))


def parse_page(body: bytes, eprint_id: str) -> Record | None:
    """Paper page -> Record from Highwire ``citation_*`` tags (``og:description`` = abstract)."""
    parser = _MetaParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    meta = parser.meta

    def first(key: str) -> str:
        return next((clean_text(v) for k, v in meta if k == key and v.strip()), "")

    title = first("citation_title")
    if not title:
        return None
    abstract = first("og:description") or None
    return Record(
        title=title,
        authors=[Author(name=clean_text(v)) for k, v in meta if k == "citation_author" and v],
        abstract=abstract,
        abstract_source="iacr_eprint" if abstract else None,
        venue=None,
        published=parse_iso_date(first("citation_publication_date")),
        eprint_id=eprint_id,
        url=PAGE_URL.format(id=eprint_id),
        kind=WorkKind.PREPRINT,
        sources={"iacr_eprint"},
    )


class IacrEprintSource(BaseSource):
    name = "iacr_eprint"
    needs_key = False
    default_interval_s = 1.0
    hosts = ("eprint.iacr.org",)

    def __init__(self, config: object, *, resolve_secret: SecretResolver | None = None) -> None:
        """``config`` is ``Config.sources.iacr_eprint``."""
        super().__init__(config, resolve_secret=resolve_secret)
        self.config = config if isinstance(config, IacrEprintConfig) else IacrEprintConfig()

    def _rss(self, http: HttpClient) -> Fetch:
        return self.fetch(http, RSS_URL, params={"order": "recent"})

    def discover(self, window: Window, profile: Profile, http: HttpClient) -> Iterator[Record]:
        result = self._rss(http)
        if not result.ok:
            return
        root = parse_xml(result.body)
        if root is None or root.find("channel") is None:
            self.error("iacr_eprint: response is not an RSS 2.0 document")
            return
        for item in root.iter("item"):
            when = parse_datetime(item.findtext("pubDate"))
            if when is None or not window.contains(when):
                continue
            link = (item.findtext("link") or "").strip()
            m = EPRINT_ID_RE.search(link) or EPRINT_ID_RE.search(item.findtext("guid") or "")
            title = element_text(item.find("title"))
            if not m or not title:
                continue
            eprint_id = m.group(1)
            abstract = element_text(item.find("description")) or None
            yield Record(
                title=title,
                authors=[
                    Author(name=clean_text(c.text))
                    for c in item.findall(f"{DC}creator")
                    if clean_text(c.text)
                ],
                abstract=abstract,
                abstract_source="iacr_eprint" if abstract else None,
                published=when.date(),
                created=when.date(),
                eprint_id=eprint_id,
                url=PAGE_URL.format(id=eprint_id),
                categories=[clean_text(c.text) for c in item.findall("category") if c.text],
                kind=WorkKind.PREPRINT,
                sources={"iacr_eprint"},
                queries={"iacr_eprint:recent"},
            )

    def metadata(self, ident: Ident, http: HttpClient) -> Record | None:
        if ident.kind is not IdentKind.EPRINT:
            return None
        m = EPRINT_ID_RE.search(ident.value)
        if not m:
            return None
        result = self.fetch(http, PAGE_URL.format(id=m.group(1)), kind="single")
        if not result.ok:
            return None
        return parse_page(result.body, m.group(1))

    def oa_pdf_candidates(self, record: Record, http: HttpClient) -> list[OaCandidate]:
        if not record.eprint_id:
            return []
        m = EPRINT_ID_RE.search(record.eprint_id)
        if not m:
            return []
        return [OaCandidate(url=PDF_URL.format(id=m.group(1)), source="eprint")]

    def test(self, http: HttpClient) -> SourceTest:
        def check(result: Fetch) -> str:
            root = parse_xml(result.body)
            return "" if root is not None and root.find("channel") is not None else "not RSS"

        return self.timed_test(lambda: self._rss(http), check)
