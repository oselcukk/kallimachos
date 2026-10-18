"""Crossref source.

api.crossref.org journals/{issn}/works with cursor paging; works/{doi}; query.bibliographic; polite
pool mailto only here.
Fixtures: ``tests/fixtures/crossref/``.

Deep paging: the first request sends ``cursor=*``, later ones the ``next-cursor`` of the previous
answer; paging stops when a page returns fewer items than ``rows`` (Crossref REST API tips).
``mailto`` is the only personal value any source sends, and only Crossref sends it, only when
``sources.crossref.mailto_ref`` is configured.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterator, Mapping
from typing import Any

from kallimachos.config.schema import CrossrefConfig
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
    date_parts,
    doi_of,
    find_arxiv,
    normalize_doi,
    parse_json,
)

API = "https://api.crossref.org"
MAX_ROWS = 1000
MAX_PAGES = 20
SELECT = (
    "DOI,title,author,container-title,created,published,published-print,published-online,"
    "issued,abstract,URL,ISSN,type,link,license,subject"
)
TYPE_KIND: dict[str, WorkKind] = {
    "journal-article": WorkKind.ARTICLE,
    "proceedings-article": WorkKind.ARTICLE,
    "posted-content": WorkKind.PREPRINT,
    "book": WorkKind.BOOK,
    "monograph": WorkKind.BOOK,
    "edited-book": WorkKind.BOOK,
    "book-chapter": WorkKind.CHAPTER,
    "book-part": WorkKind.CHAPTER,
    "dissertation": WorkKind.THESIS,
}


def parse_work(item: Mapping[str, Any]) -> Record | None:
    titles = item.get("title") or []
    title = clean_text(titles[0]) if titles else ""
    doi = normalize_doi(item.get("DOI"))
    if not title or not doi:
        return None
    authors: list[Author] = []
    for a in item.get("author") or []:
        name = clean_text(" ".join(x for x in (a.get("given"), a.get("family")) if x))
        name = name or clean_text(a.get("name"))
        if name:
            orcid = (a.get("ORCID") or "").rsplit("/", 1)[-1] or None
            authors.append(Author(name=name, orcid=orcid))
    abstract = clean_text(item.get("abstract")) or None
    if abstract and abstract.lower().startswith("abstract "):
        abstract = abstract[len("abstract ") :]
    published = (
        date_parts(item.get("published"))
        or date_parts(item.get("published-print"))
        or date_parts(item.get("published-online"))
        or date_parts(item.get("issued"))
    )
    venues = item.get("container-title") or []
    arxiv = find_arxiv(doi)
    return Record(
        title=title,
        authors=authors,
        abstract=abstract,
        abstract_source="crossref" if abstract else None,
        venue=clean_text(venues[0]) if venues else None,
        issn=[str(x) for x in item.get("ISSN") or []],
        published=published,
        created=date_parts(item.get("created")),
        doi=doi,
        arxiv_id=arxiv[0] if arxiv else None,
        url=f"https://doi.org/{doi}",
        keywords=[clean_text(s) for s in item.get("subject") or [] if s],
        kind=TYPE_KIND.get(str(item.get("type") or ""), WorkKind.UNKNOWN),
        sources={"crossref"},
    )


def full_journal_issns(profile: Profile) -> list[str]:
    """ISSNs of ``mode: full`` journals (topic-filtered journals are covered by OpenAlex)."""
    out: list[str] = []
    for ch in profile.channels:
        for j in ch.journals:
            if j.mode != "full":
                continue
            for issn in j.issn[:1]:
                if issn and issn not in out:
                    out.append(issn)
    return out


class CrossrefSource(BaseSource):
    name = "crossref"
    needs_key = False
    default_interval_s = 0.6
    hosts = ("api.crossref.org",)

    def __init__(self, config: object, *, resolve_secret: SecretResolver | None = None) -> None:
        """``config`` is ``Config.sources.crossref``."""
        super().__init__(config, resolve_secret=resolve_secret)
        self.config = config if isinstance(config, CrossrefConfig) else CrossrefConfig()

    def key_refs(self) -> dict[str, str | None]:
        return {"mailto": self.config.mailto_ref}

    def _polite(self, params: dict[str, str]) -> dict[str, str]:
        mailto = self.secret(self.config.mailto_ref, "mailto")
        if mailto:
            params["mailto"] = mailto
        return params

    def discover(self, window: Window, profile: Profile, http: HttpClient) -> Iterator[Record]:
        issns = full_journal_issns(profile)
        if not issns:
            return
        index_from = window.start.astimezone(_dt.UTC).date().isoformat()
        created_from = (
            (window.end - _dt.timedelta(days=self.config.created_window_days))
            .astimezone(_dt.UTC)
            .date()
            .isoformat()
        )
        rows = max(1, min(self.config.rows, MAX_ROWS))
        flt = f"from-index-date:{index_from},from-created-date:{created_from},type:journal-article"
        seen: set[str] = set()
        for issn in issns:
            for item in self._pages(http, f"{API}/journals/{issn}/works", flt, rows):
                rec = parse_work(item)
                if rec is None or rec.doi in seen:
                    continue
                seen.add(rec.doi or "")
                if issn not in rec.issn:
                    rec.issn.append(issn)
                rec.queries = {f"crossref:journal:{issn}"}
                yield rec

    def _pages(
        self, http: HttpClient, url: str, flt: str, rows: int
    ) -> Iterator[Mapping[str, Any]]:
        cursor = "*"
        for _page in range(MAX_PAGES):
            params = self._polite(
                {"filter": flt, "rows": str(rows), "cursor": cursor, "select": SELECT}
            )
            result = self.fetch(http, url, params=params)
            if not result.ok:
                return
            data = parse_json(result.body)
            message = data.get("message") if isinstance(data, dict) else None
            if not isinstance(message, dict):
                self.error("crossref: unexpected response shape")
                return
            items = message.get("items") or []
            yield from items
            next_cursor = message.get("next-cursor")
            if len(items) < rows or not next_cursor or next_cursor == cursor:
                return
            cursor = str(next_cursor)

    def _work(self, http: HttpClient, doi: str) -> Mapping[str, Any] | None:
        result = self.fetch(http, f"{API}/works/{doi}", params=self._polite({}), kind="single")
        if not result.ok:
            return None
        data = parse_json(result.body)
        message = data.get("message") if isinstance(data, dict) else None
        return message if isinstance(message, dict) else None

    def metadata(self, ident: Ident, http: HttpClient) -> Record | None:
        doi = doi_of(ident)
        if not doi:
            return None
        item = self._work(http, doi)
        return parse_work(item) if item else None

    def oa_pdf_candidates(self, record: Record, http: HttpClient) -> list[OaCandidate]:
        """PDF links of works under an open licence (Creative Commons); nothing else."""
        if not record.doi:
            return []
        item = self._work(http, record.doi)
        if not item:
            return []
        licences = [
            str(lic.get("URL") or "")
            for lic in item.get("license") or []
            if "creativecommons.org" in str(lic.get("URL") or "")
        ]
        if not licences:
            return []
        out: list[OaCandidate] = []
        for link in item.get("link") or []:
            url = str(link.get("URL") or "")
            if url and "pdf" in str(link.get("content-type") or "").lower():
                out.append(OaCandidate(url=url, source="crossref", license=licences[0]))
        return out

    def test(self, http: HttpClient) -> SourceTest:
        def check(result: Fetch) -> str:
            data = parse_json(result.body)
            ok = isinstance(data, dict) and data.get("status") == "ok"
            return "" if ok else "unexpected Crossref response"

        params = self._polite({"rows": "1", "select": "DOI"})
        return self.timed_test(lambda: self.fetch(http, f"{API}/works", params=params), check)
