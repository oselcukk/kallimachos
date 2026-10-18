"""RSS/Atom source.

user feeds; stdlib xml.etree parser for RSS 2.0 and Atom; channel_hint per feed.
Fixtures: ``tests/fixtures/rss/``.

RSS 1.0 (RDF) is read as well because many journal feeds use it. Real-world quirks handled:
unescaped HTML/JATS inside ``<description>`` (all inner text is kept), ISO dates in ``pubDate``,
``dc:date`` instead of ``pubDate``. Items without any date are kept (the feed decides what is
recent); dated items outside the window are dropped. A feed's ``channel_hint`` is carried in the
record's ``queries`` as ``channel_hint:<id>``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from kallimachos.config.schema import RssConfig, RssFeed
from kallimachos.interfaces import (
    Author,
    HttpClient,
    Ident,
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
    find_arxiv,
    find_doi,
    parse_datetime,
    parse_xml,
)

ATOM = "{http://www.w3.org/2005/Atom}"
DC = "{http://purl.org/dc/elements/1.1/}"
RSS1 = "{http://purl.org/rss/1.0/}"
RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
PRISM = "{http://prismstandard.org/namespaces/basic/2.0/}"


@dataclass
class FeedItem:
    title: str
    link: str
    summary: str
    authors: list[str]
    when: str | None
    ident_texts: list[str]


def _text(el: ET.Element, *tags: str) -> str:
    for tag in tags:
        found = el.find(tag)
        if found is not None:
            text = element_text(found)
            if text:
                return text
    return ""


def parse_feed(root: ET.Element) -> list[FeedItem] | None:
    """Items of an RSS 2.0, RSS 1.0 (RDF) or Atom document; ``None`` for anything else."""
    items: list[FeedItem] = []
    if root.tag == f"{ATOM}feed":
        for e in root.findall(f"{ATOM}entry"):
            link = ""
            for ln in e.findall(f"{ATOM}link"):
                if ln.get("rel", "alternate") == "alternate" and ln.get("href"):
                    link = ln.get("href", "")
                    break
            others = [ln.get("href", "") for ln in e.findall(f"{ATOM}link")]
            items.append(
                FeedItem(
                    title=_text(e, f"{ATOM}title"),
                    link=link,
                    summary=_text(e, f"{ATOM}summary", f"{ATOM}content"),
                    authors=[
                        clean_text(a.findtext(f"{ATOM}name"))
                        for a in e.findall(f"{ATOM}author")
                        if clean_text(a.findtext(f"{ATOM}name"))
                    ],
                    when=e.findtext(f"{ATOM}published") or e.findtext(f"{ATOM}updated"),
                    ident_texts=[
                        e.findtext(f"{ATOM}id") or "",
                        *others,
                        e.findtext("{http://arxiv.org/schemas/atom}doi") or "",
                    ],
                )
            )
        return items
    if root.tag == "rss":
        nodes = root.findall("channel/item")
    elif root.tag == f"{RDF}RDF":
        nodes = root.findall(f"{RSS1}item")
    else:
        return None
    for it in nodes:
        link = _text(it, "link", f"{RSS1}link")
        authors = [clean_text(c.text) for c in it.findall(f"{DC}creator") if clean_text(c.text)]
        if not authors and it.findtext("author"):
            authors = [clean_text(it.findtext("author"))]
        items.append(
            FeedItem(
                title=_text(it, "title", f"{RSS1}title"),
                link=link,
                summary=_text(it, "description", f"{RSS1}description", f"{ATOM}summary"),
                authors=authors,
                when=it.findtext("pubDate")
                or it.findtext(f"{DC}date")
                or it.findtext(f"{PRISM}publicationDate"),
                ident_texts=[
                    it.findtext("guid") or "",
                    it.findtext(f"{DC}identifier") or "",
                    it.findtext(f"{PRISM}doi") or "",
                    link,
                    it.get(f"{RDF}about") or "",
                ],
            )
        )
    return items


def item_record(item: FeedItem, feed: RssFeed) -> Record | None:
    if not item.title:
        return None
    doi = find_doi(*item.ident_texts)
    arxiv = find_arxiv(*item.ident_texts)
    when = parse_datetime(item.when)
    queries = {f"rss:{feed.name}"}
    if feed.channel_hint:
        queries.add(f"channel_hint:{feed.channel_hint}")
    return Record(
        title=item.title,
        authors=[Author(name=a) for a in item.authors],
        abstract=item.summary or None,
        abstract_source=f"rss:{feed.name}" if item.summary else None,
        venue=None,
        published=when.date() if when else None,
        created=when.date() if when else None,
        doi=doi,
        arxiv_id=arxiv[0] if arxiv else None,
        url=item.link or (f"https://doi.org/{doi}" if doi else None),
        kind=WorkKind.PREPRINT if arxiv and not doi else WorkKind.UNKNOWN,
        sources={"rss"},
        queries=queries,
    )


class RssSource(BaseSource):
    name = "rss"
    needs_key = False
    default_interval_s = 1.0

    def __init__(self, config: object, *, resolve_secret: SecretResolver | None = None) -> None:
        """``config`` is ``Config.sources.rss``."""
        super().__init__(config, resolve_secret=resolve_secret)
        self.config = config if isinstance(config, RssConfig) else RssConfig()
        self.hosts = tuple(
            sorted({urlsplit(f.url).hostname or "" for f in self.config.feeds} - {""})
        )

    def _feed(self, http: HttpClient, feed: RssFeed) -> list[FeedItem] | None:
        result = self.fetch(http, feed.url)
        if not result.ok:
            return None
        root = parse_xml(result.body)
        items = parse_feed(root) if root is not None else None
        if items is None:
            self.error(f"rss: feed {feed.name} is not RSS or Atom")
        return items

    def discover(self, window: Window, profile: Profile, http: HttpClient) -> Iterator[Record]:
        for feed in self.config.feeds:
            for item in self._feed(http, feed) or []:
                when = parse_datetime(item.when)
                if when is not None and not window.contains(when):
                    continue
                rec = item_record(item, feed)
                if rec is not None:
                    yield rec

    def metadata(self, ident: Ident, http: HttpClient) -> Record | None:
        """Feeds have no lookup by identifier."""
        return None

    def oa_pdf_candidates(self, record: Record, http: HttpClient) -> list[OaCandidate]:
        if record.url and record.url.lower().split("?", 1)[0].endswith(".pdf"):
            return [OaCandidate(url=record.url, source="rss")]
        return []

    def test(self, http: HttpClient) -> SourceTest:
        if not self.config.feeds:
            return SourceTest(ok=False, message="no feeds configured", source=self.name)
        feed = self.config.feeds[0]

        def check(result: Fetch) -> str:
            root = parse_xml(result.body)
            return "" if root is not None and parse_feed(root) is not None else "not RSS/Atom"

        out = self.timed_test(lambda: self.fetch(http, feed.url), check)
        out.details["feed"] = feed.name
        return out
