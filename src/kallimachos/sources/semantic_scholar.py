"""Semantic Scholar source.

graph/v1 paper search / bulk / match; recommendations; x-api-key header; 429 closes the source for
the run.
Fixtures: ``tests/fixtures/semantic_scholar/``.

Discovery uses ``/paper/search/bulk`` (1000 per call, ``token`` continuation) with the channel
core terms OR-ed (``|``) and ``publicationDateOrYear`` set to the window. The key goes only in the
case-sensitive ``x-api-key`` header.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterator, Mapping
from typing import Any

from kallimachos.config.schema import SemanticScholarConfig
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
    chunked,
    clean_text,
    core_terms,
    find_arxiv,
    normalize_doi,
    parse_iso_date,
    parse_json,
)

API = "https://api.semanticscholar.org/graph/v1"
FIELDS = (
    "title,authors,abstract,externalIds,venue,publicationDate,year,openAccessPdf,"
    "publicationTypes,journal"
)
MAX_PAGES = 3
TERMS_PER_QUERY = 20


def parse_paper(p: Mapping[str, Any]) -> Record | None:
    title = clean_text(p.get("title"))
    pid = p.get("paperId")
    if not title or not pid:
        return None
    ext = p.get("externalIds") or {}
    doi = normalize_doi(ext.get("DOI"))
    arxiv = ext.get("ArXiv")
    if not arxiv:
        hit = find_arxiv(doi)
        arxiv = hit[0] if hit else None
    journal = p.get("journal") or {}
    venue = clean_text(journal.get("name") or p.get("venue")) or None
    types = [str(t) for t in p.get("publicationTypes") or []]
    if "JournalArticle" in types or "Conference" in types:
        kind = WorkKind.ARTICLE
    elif "Book" in types:
        kind = WorkKind.BOOK
    elif arxiv and not doi:
        kind = WorkKind.PREPRINT
    else:
        kind = WorkKind.UNKNOWN
    published = parse_iso_date(p.get("publicationDate"))
    if published is None and p.get("year"):
        published = parse_iso_date(str(p["year"]))
    abstract = clean_text(p.get("abstract")) or None
    return Record(
        title=title,
        authors=[
            Author(name=clean_text(a.get("name"))) for a in p.get("authors") or [] if a.get("name")
        ],
        abstract=abstract,
        abstract_source="semantic_scholar" if abstract else None,
        venue=venue,
        published=published,
        doi=doi,
        arxiv_id=str(arxiv) if arxiv else None,
        pubmed_id=str(ext["PubMed"]) if ext.get("PubMed") else None,
        s2_id=str(pid),
        url=f"https://www.semanticscholar.org/paper/{pid}",
        kind=kind,
        sources={"semantic_scholar"},
    )


def paper_path(ident: Ident) -> str | None:
    if ident.kind is IdentKind.DOI:
        return f"DOI:{normalize_doi(ident.value) or ident.value}"
    if ident.kind is IdentKind.ARXIV:
        return f"ARXIV:{ident.value}"
    if ident.kind is IdentKind.PUBMED:
        return f"PMID:{ident.value}"
    if ident.kind is IdentKind.S2:
        return ident.value
    return None


class SemanticScholarSource(BaseSource):
    name = "semantic_scholar"
    needs_key = False
    default_interval_s = 1.2
    hosts = ("api.semanticscholar.org",)

    def __init__(self, config: object, *, resolve_secret: SecretResolver | None = None) -> None:
        """``config`` is ``Config.sources.semantic_scholar``."""
        super().__init__(config, resolve_secret=resolve_secret)
        self.config = (
            config if isinstance(config, SemanticScholarConfig) else SemanticScholarConfig()
        )
        self.closed = False

    def key_refs(self) -> dict[str, str | None]:
        return {"api_key": self.config.api_key_ref}

    def reset(self) -> None:
        super().reset()
        self.closed = False

    def _headers(self) -> dict[str, str]:
        key = self.secret(self.config.api_key_ref, "api_key")
        return {"x-api-key": key} if key else {}

    def _get(self, http: HttpClient, url: str, params: dict[str, str], kind: str) -> Fetch | None:
        if self.closed:
            return None
        result = self.fetch(http, url, params=params, headers=self._headers(), kind=kind)
        if result.status == 429:
            self.closed = True
            self.error("semantic_scholar: HTTP 429; source closed for this run")
        return result

    def discover(self, window: Window, profile: Profile, http: HttpClient) -> Iterator[Record]:
        last_day = (window.end - _dt.timedelta(microseconds=1)).astimezone(_dt.UTC).date()
        dates = f"{window.start.astimezone(_dt.UTC).date().isoformat()}:{last_day.isoformat()}"
        seen: dict[str, Record] = {}
        for channel_id, terms in core_terms(profile).items():
            for group in chunked(terms, TERMS_PER_QUERY):
                query = " | ".join(f'"{t.replace(chr(34), "")}"' for t in group)
                tag = f"semantic_scholar:keywords:{channel_id}"
                for paper in self._bulk(http, query, dates):
                    rec = parse_paper(paper)
                    if rec is None or rec.s2_id is None:
                        continue
                    if rec.s2_id in seen:
                        seen[rec.s2_id].queries.add(tag)
                        continue
                    rec.queries = {tag}
                    seen[rec.s2_id] = rec
                if self.closed:
                    yield from seen.values()
                    return
        yield from seen.values()

    def _bulk(self, http: HttpClient, query: str, dates: str) -> Iterator[Mapping[str, Any]]:
        token: str | None = None
        for _page in range(MAX_PAGES):
            params = {"query": query, "publicationDateOrYear": dates, "fields": FIELDS}
            if token:
                params["token"] = token
            result = self._get(http, f"{API}/paper/search/bulk", params, "search")
            if result is None or not result.ok:
                return
            data = parse_json(result.body)
            if not isinstance(data, dict):
                self.error("semantic_scholar: unexpected response shape")
                return
            yield from data.get("data") or []
            token = data.get("token")
            if not token:
                return

    def _paper(self, http: HttpClient, ident: Ident) -> Mapping[str, Any] | None:
        path = paper_path(ident)
        if path is None:
            return None
        result = self._get(http, f"{API}/paper/{path}", {"fields": FIELDS}, "single")
        if result is None or not result.ok:
            return None
        data = parse_json(result.body)
        return data if isinstance(data, dict) else None

    def metadata(self, ident: Ident, http: HttpClient) -> Record | None:
        paper = self._paper(http, ident)
        return parse_paper(paper) if paper else None

    def oa_pdf_candidates(self, record: Record, http: HttpClient) -> list[OaCandidate]:
        for kind, value in (
            (IdentKind.S2, record.s2_id),
            (IdentKind.DOI, record.doi),
            (IdentKind.ARXIV, record.arxiv_id),
        ):
            if value:
                paper = self._paper(http, Ident(kind, value))
                break
        else:
            return []
        if not paper:
            return []
        pdf = paper.get("openAccessPdf") or {}
        url = str(pdf.get("url") or "")
        if not url:
            return []
        arxiv = find_arxiv(url)
        return [
            OaCandidate(
                url=url,
                source="semantic_scholar",
                version=arxiv[1] if arxiv else None,
                license=pdf.get("license"),
            )
        ]

    def test(self, http: HttpClient) -> SourceTest:
        def check(result: Fetch) -> str:
            data = parse_json(result.body)
            return "" if isinstance(data, dict) and "data" in data else "unexpected response"

        params = {"query": "modular forms", "limit": "1", "fields": "title"}

        def run() -> Fetch:
            result = self._get(http, f"{API}/paper/search", params, "search")
            return result if result is not None else Fetch(response=None)

        out = self.timed_test(run, check)
        out.key_present = bool(self._headers())
        return out
