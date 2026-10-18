"""OpenAlex source.

api.openalex.org works with filter/per-page/cursor/select; key sent in Authorization header only;
budget via http.budget; single OpenAlex job lock.
Fixtures: ``tests/fixtures/openalex/``.

Request kinds: ``single`` for ``/works/<id>`` (free), ``list`` for filter queries,
``search`` for full-text search. Before every paid call the source asks the budget gate
(``http.budget.Budget.can_spend``); ``meta.cost_usd`` of each answer is summed in ``cost_usd``.
``from_created_date`` is a premium filter: it is used only with ``premium_filters: true``,
otherwise ``from_publication_date``. Searches with more than five boolean operators
are spaced at least one second apart.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Protocol

from kallimachos.config.schema import OpenAlexConfig
from kallimachos.http.budget import Budget
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
    core_terms,
    find_arxiv,
    normalize_doi,
    parse_iso_date,
    parse_json,
)

API = "https://api.openalex.org"
SELECT = (
    "id,doi,display_name,authorships,primary_location,locations,best_oa_location,"
    "publication_date,created_date,abstract_inverted_index,referenced_works,type,ids"
)
OR_GROUP = 50
TERMS_PER_SEARCH = 20
VENUE_TYPES = "primary_location.source.type:journal|conference|book series"
TYPE_KIND: dict[str, WorkKind] = {
    "article": WorkKind.ARTICLE,
    "review": WorkKind.ARTICLE,
    "letter": WorkKind.ARTICLE,
    "preprint": WorkKind.PREPRINT,
    "book": WorkKind.BOOK,
    "book-chapter": WorkKind.CHAPTER,
    "dissertation": WorkKind.THESIS,
}


class BudgetGate(Protocol):
    """The part of ``kallimachos.http.budget.Budget`` this source needs."""

    def can_spend(self, kind: str, *, weekly: bool = False) -> bool: ...


def boolean_operators(query: str) -> int:
    """Number of upper-case AND/OR/NOT operators (more than five force a 1 s interval); the
    counter lives in ``http.budget.Budget`` (one regex for the client and the source)."""
    return Budget.boolean_operator_count(query)


def short_id(value: str | None) -> str | None:
    """``https://openalex.org/W123`` -> ``W123``."""
    if not value:
        return None
    return value.rstrip("/").rsplit("/", 1)[-1] or None


def abstract_from_index(index: Mapping[str, list[int]] | None) -> str | None:
    if not index:
        return None
    slots: dict[int, str] = {}
    for word, positions in index.items():
        for pos in positions:
            slots[int(pos)] = word
    text = " ".join(slots[i] for i in sorted(slots))
    return clean_text(text) or None


def parse_work(w: Mapping[str, Any]) -> Record | None:
    """One OpenAlex ``work`` object -> :class:`Record` (data, never instructions). The single
    OpenAlex normaliser: ``profile.match.record_from_openalex`` adds its profile extras on top.
    ``None`` when the work has no title or id."""
    title = clean_text(w.get("display_name") or w.get("title"))
    wid = short_id(w.get("id"))
    if not title or not wid:
        return None
    doi = normalize_doi(w.get("doi"))
    authors: list[Author] = []
    for a in w.get("authorships") or []:
        au = a.get("author") or {}
        name = clean_text(au.get("display_name") or a.get("raw_author_name"))
        if name:
            authors.append(
                Author(
                    name=name,
                    orcid=short_id(au.get("orcid")),
                    openalex_id=short_id(au.get("id")),
                )
            )
    primary = w.get("primary_location") or {}
    source = primary.get("source") or {}
    arxiv = None
    for loc in w.get("locations") or []:
        arxiv = find_arxiv(loc.get("landing_page_url"), loc.get("pdf_url"))
        if arxiv:
            break
    if arxiv is None:
        arxiv = find_arxiv(doi)
    ids = w.get("ids") or {}
    pmid = short_id(ids.get("pmid"))
    abstract = abstract_from_index(w.get("abstract_inverted_index"))
    issn: list[str] = []
    for value in [*(source.get("issn") or []), source.get("issn_l")]:
        if value and str(value) not in issn:
            issn.append(str(value))
    published = parse_iso_date(w.get("publication_date"))
    if published is None and isinstance(w.get("publication_year"), int):
        published = _dt.date(int(w["publication_year"]), 1, 1)
    keywords: list[str] = []
    for t in w.get("topics") or []:
        name = clean_text(t.get("display_name")) if isinstance(t, dict) else ""
        if name and name not in keywords:
            keywords.append(name)
    for k in w.get("keywords") or []:
        name = clean_text(k.get("display_name")) if isinstance(k, dict) else clean_text(str(k))
        if name and name not in keywords:
            keywords.append(name)
    return Record(
        title=title,
        authors=authors,
        abstract=abstract,
        abstract_source="openalex" if abstract else None,
        venue=clean_text(source.get("display_name")) or None,
        venue_openalex_id=short_id(source.get("id")),
        issn=issn,
        published=published,
        created=parse_iso_date(w.get("created_date")),
        doi=doi,
        arxiv_id=arxiv[0] if arxiv else None,
        openalex_id=wid,
        pubmed_id=pmid,
        url=f"https://doi.org/{doi}" if doi else (primary.get("landing_page_url") or w.get("id")),
        keywords=keywords,
        referenced_works=[x for x in (short_id(r) for r in w.get("referenced_works") or []) if x],
        kind=work_kind(
            str(w.get("type") or ""),
            str(w.get("type_crossref") or ""),
            arxiv_doi=bool(doi and doi.startswith(ARXIV_DOI_PREFIX.rstrip("."))),
        ),
        sources={"openalex"},
    )


def work_kind(work_type: str, type_crossref: str = "", *, arxiv_doi: bool = False) -> WorkKind:
    """OpenAlex ``type`` (+ ``type_crossref``) -> :class:`WorkKind`. A work whose own DOI is an
    arXiv DOI is a preprint; a journal article that also has an arXiv mirror location is not."""
    if arxiv_doi or work_type == "preprint" or type_crossref == "posted-content":
        return WorkKind.PREPRINT
    return TYPE_KIND.get(work_type, WorkKind.UNKNOWN)


class OpenAlexSource(BaseSource):
    name = "openalex"
    needs_key = False
    default_interval_s = 0.15
    hosts = ("api.openalex.org",)

    def __init__(
        self,
        config: object,
        *,
        resolve_secret: SecretResolver | None = None,
        budget: BudgetGate | None = None,
        weekly: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """``config`` is ``Config.sources.openalex``."""
        super().__init__(config, resolve_secret=resolve_secret)
        self.config = config if isinstance(config, OpenAlexConfig) else OpenAlexConfig()
        self.budget = budget
        self.weekly = weekly
        self._sleep = sleep
        self._clock = clock
        self._last_slow: float | None = None
        self.cost_usd = 0.0
        self.stopped = False

    def key_refs(self) -> dict[str, str | None]:
        return {"api_key": self.config.api_key_ref}

    def reset(self) -> None:
        super().reset()
        self.cost_usd = 0.0
        self.stopped = False

    # -- requests ------------------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        key = self.secret(self.config.api_key_ref, "api_key")
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _get(self, http: HttpClient, url: str, params: dict[str, str], kind: str) -> Fetch | None:
        """One call with the budget gate, slow-search spacing and the 429 stop rule."""
        if self.stopped:
            return None
        if (
            kind != "single"
            and self.budget is not None
            and not self.budget.can_spend(kind, weekly=self.weekly)
        ):
            self.error("openalex: daily budget exhausted; remaining queries skipped")
            self.stopped = True
            return None
        query = " ".join(v for k, v in params.items() if k in ("search", "filter"))
        if kind == "search" and boolean_operators(query) > 5:
            now = self._clock()
            if self._last_slow is not None and now - self._last_slow < 1.0:
                self._sleep(1.0 - (now - self._last_slow))
            self._last_slow = self._clock()
        result = self.fetch(http, url, params=params, headers=self._headers(), kind=kind)
        if result.status == 429:
            self.error("openalex: HTTP 429; source stopped for this run")
            self.stopped = True
        if result.ok:
            data = parse_json(result.body)
            meta = data.get("meta") if isinstance(data, dict) else None
            if isinstance(meta, dict):
                with contextlib.suppress(TypeError, ValueError):
                    self.cost_usd += float(meta.get("cost_usd") or 0.0)
        return result

    def works(
        self, http: HttpClient, flt: str, *, search: str | None = None, max_pages: int | None = None
    ) -> Iterator[Mapping[str, Any]]:
        """Cursor-paged ``/works`` list."""
        kind = "search" if (search or ".search:" in flt) else "list"
        per_page = max(1, min(self.config.per_page, 200))
        cursor = "*"
        for _page in range(max_pages or self.config.max_pages):
            params = {"filter": flt, "per-page": str(per_page), "cursor": cursor, "select": SELECT}
            if search:
                params["search"] = search
            result = self._get(http, f"{API}/works", params, kind)
            if result is None or not result.ok:
                return
            data = parse_json(result.body)
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                self.error("openalex: unexpected response shape")
                return
            results = data["results"]
            yield from results
            nxt = (data.get("meta") or {}).get("next_cursor")
            if not nxt or len(results) < per_page:
                return
            cursor = str(nxt)

    # -- plug-in API ---------------------------------------------------------------------------

    def date_filter(self, window: Window) -> str:
        day = window.start.astimezone(_dt.UTC).date().isoformat()
        if self.config.premium_filters:
            return f"from_created_date:{day}"
        return f"from_publication_date:{day}"

    def queries(self, window: Window, profile: Profile) -> list[tuple[str, str]]:
        """``(query tag, filter)`` pairs for one scan."""
        date_f = self.date_filter(window)
        out: list[tuple[str, str]] = []
        full: list[str] = []
        topic: list[str] = []
        for ch in profile.channels:
            for j in ch.journals:
                sid = short_id(j.openalex_source_id)
                if not sid:
                    continue
                target = full if j.mode == "full" else topic
                if sid not in target:
                    target.append(sid)
        for group in chunked(sorted(full), OR_GROUP):
            out.append(
                ("openalex:journal_full", f"primary_location.source.id:{'|'.join(group)},{date_f}")
            )
        for group in chunked(sorted(topic), OR_GROUP):
            out.append(
                ("openalex:journal_topic", f"primary_location.source.id:{'|'.join(group)},{date_f}")
            )
        author_ids = [a.openalex_id for a in profile.authors.always_show]
        author_ids += [a.openalex_id for a in profile.authors.follow]
        authors = sorted({sid for a in author_ids if (sid := short_id(a))})
        for group in chunked(authors, OR_GROUP):
            out.append(("openalex:author", f"authorships.author.id:{'|'.join(group)},{date_f}"))
        seeds = sorted({sid for s in profile.seeds if (sid := short_id(s.openalex_id))})
        for group in chunked(seeds, OR_GROUP):
            out.append(("openalex:seed_citation", f"cites:{'|'.join(group)},{date_f}"))
        for channel_id, terms in core_terms(profile).items():
            usable = [t.replace('"', "").replace(",", " ") for t in terms if len(t) >= 4]
            for group in chunked(usable, TERMS_PER_SEARCH):
                expr = " OR ".join(f'"{t}"' for t in group)
                out.append(
                    (
                        f"openalex:keywords:{channel_id}",
                        f"title_and_abstract.search:{expr},{VENUE_TYPES},{date_f}",
                    )
                )
        return out

    def discover(self, window: Window, profile: Profile, http: HttpClient) -> Iterator[Record]:
        seen: dict[str, Record] = {}
        for tag, flt in self.queries(window, profile):
            if self.stopped:
                break
            for w in self.works(http, flt):
                rec = parse_work(w)
                if rec is None or rec.openalex_id is None:
                    continue
                if rec.openalex_id in seen:
                    seen[rec.openalex_id].queries.add(tag)
                    continue
                rec.queries = {tag}
                seen[rec.openalex_id] = rec
        yield from seen.values()

    def _single(self, http: HttpClient, ident: Ident) -> Mapping[str, Any] | None:
        if ident.kind is IdentKind.OPENALEX:
            path = short_id(ident.value) or ident.value
        elif ident.kind is IdentKind.DOI:
            path = f"doi:{normalize_doi(ident.value) or ident.value}"
        elif ident.kind is IdentKind.ARXIV:
            path = f"doi:10.48550/arxiv.{ident.value.lower()}"
        elif ident.kind is IdentKind.PUBMED:
            path = f"pmid:{ident.value}"
        else:
            return None
        result = self._get(http, f"{API}/works/{path}", {}, "single")
        if result is None or not result.ok:
            return None
        data = parse_json(result.body)
        return data if isinstance(data, dict) else None

    def metadata(self, ident: Ident, http: HttpClient) -> Record | None:
        w = self._single(http, ident)
        return parse_work(w) if w else None

    def oa_pdf_candidates(self, record: Record, http: HttpClient) -> list[OaCandidate]:
        ident: Ident | None = None
        if record.openalex_id:
            ident = Ident(IdentKind.OPENALEX, record.openalex_id)
        elif record.doi:
            ident = Ident(IdentKind.DOI, record.doi)
        if ident is None:
            return []
        w = self._single(http, ident)
        if not w:
            return []
        locations = list(w.get("locations") or [])
        best = w.get("best_oa_location")
        if isinstance(best, dict):
            locations.insert(0, best)
        out: list[OaCandidate] = []
        seen: set[str] = set()
        for loc in locations:
            if not isinstance(loc, dict) or not loc.get("is_oa"):
                continue
            pdf = loc.get("pdf_url")
            landing = loc.get("landing_page_url")
            url = pdf or landing
            if not url or url in seen:
                continue
            seen.add(url)
            arxiv = find_arxiv(url)
            out.append(
                OaCandidate(
                    url=str(url),
                    source="openalex",
                    version=arxiv[1] if arxiv else None,
                    license=loc.get("license"),
                    is_pdf=bool(pdf),
                )
            )
        return out

    def test(self, http: HttpClient) -> SourceTest:
        def check(result: Fetch) -> str:
            data = parse_json(result.body)
            return "" if isinstance(data, dict) and "results" in data else "unexpected response"

        yesterday = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=1)).date().isoformat()
        params = {"filter": f"from_publication_date:{yesterday}", "per-page": "1", "select": "id"}

        def run() -> Fetch:
            result = self._get(http, f"{API}/works", params, "list")
            return result if result is not None else Fetch(response=None)

        out = self.timed_test(run, check)
        out.key_present = bool(self._headers())
        out.details["cost_usd"] = round(self.cost_usd, 6)
        return out
