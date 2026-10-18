"""PubMed source.

E-utilities esearch + efetch with tool/email; 0.1 s interval with api_key.
Fixtures: ``tests/fixtures/pubmed/``.

Every call carries ``tool`` (``sources.pubmed.tool``) and, when configured, ``email``; NCBI asks
that both are registered (README step). The optional ``api_key`` is a query parameter by NCBI's
design; it is passed in ``params`` (never formatted into the URL) so the HTTP layer can keep it
out of cache keys and logs. The entry-date window uses ``mindate``/``maxdate`` with
``datetype=edat``.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterator
from xml.etree import ElementTree as ET

from kallimachos.config.schema import PubmedConfig
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
    element_text,
    normalize_doi,
    parse_json,
    parse_xml,
)

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
RETMAX = 500
FETCH_BATCH = 200
TERMS_PER_QUERY = 20
MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}


def _ymd(el: ET.Element | None) -> _dt.date | None:
    if el is None:
        return None
    year = (el.findtext("Year") or "").strip()
    if not year.isdigit():
        medline = (el.findtext("MedlineDate") or "").strip()[:4]
        if not medline.isdigit():
            return None
        year = medline
    month_raw = (el.findtext("Month") or "1").strip()
    month = int(month_raw) if month_raw.isdigit() else MONTHS.get(month_raw[:3].lower(), 1)
    day_raw = (el.findtext("Day") or "1").strip()
    day = int(day_raw) if day_raw.isdigit() else 1
    try:
        return _dt.date(int(year), month, day)
    except ValueError:
        return None


def parse_article(art: ET.Element) -> Record | None:
    cit = art.find("MedlineCitation")
    if cit is None:
        return None
    pmid = (cit.findtext("PMID") or "").strip()
    article = cit.find("Article")
    if not pmid or article is None:
        return None
    title = element_text(article.find("ArticleTitle")).rstrip(".")
    if not title:
        return None
    parts: list[str] = []
    for ab in article.findall("Abstract/AbstractText"):
        text = element_text(ab)
        label = ab.get("Label")
        if text:
            parts.append(f"{label}: {text}" if label else text)
    abstract = " ".join(parts) or None
    authors: list[Author] = []
    for au in article.findall("AuthorList/Author"):
        name = clean_text(f"{au.findtext('ForeName') or ''} {au.findtext('LastName') or ''}")
        name = name or clean_text(au.findtext("CollectiveName"))
        if name:
            orcid = next(
                (
                    clean_text(i.text).rsplit("/", 1)[-1]
                    for i in au.findall("Identifier")
                    if i.get("Source") == "ORCID" and i.text
                ),
                None,
            )
            authors.append(Author(name=name, orcid=orcid))
    doi = None
    for aid in art.findall("PubmedData/ArticleIdList/ArticleId"):
        if aid.get("IdType") == "doi":
            doi = normalize_doi(aid.text)
    if doi is None:
        for eloc in article.findall("ELocationID"):
            if eloc.get("EIdType") == "doi":
                doi = normalize_doi(eloc.text)
    types = {
        clean_text(t.text).lower() for t in article.findall("PublicationTypeList/PublicationType")
    }
    published = _ymd(article.find("ArticleDate")) or _ymd(
        article.find("Journal/JournalIssue/PubDate")
    )
    entrez = next(
        (
            _ymd(d)
            for d in art.findall("PubmedData/History/PubMedPubDate")
            if d.get("PubStatus") == "entrez"
        ),
        None,
    )
    return Record(
        title=title,
        authors=authors,
        abstract=abstract,
        abstract_source="pubmed" if abstract else None,
        venue=clean_text(article.findtext("Journal/Title")) or None,
        issn=[clean_text(i.text) for i in article.findall("Journal/ISSN") if i.text],
        published=published,
        created=entrez,
        doi=doi,
        pubmed_id=pmid,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        keywords=[clean_text(k.text) for k in cit.findall("KeywordList/Keyword") if k.text],
        kind=WorkKind.PREPRINT if "preprint" in types else WorkKind.ARTICLE,
        sources={"pubmed"},
    )


def pmc_id(art: ET.Element) -> str | None:
    for aid in art.findall("PubmedData/ArticleIdList/ArticleId"):
        if aid.get("IdType") == "pmc" and aid.text:
            return aid.text.strip()
    return None


class PubmedSource(BaseSource):
    name = "pubmed"
    needs_key = False
    default_interval_s = 0.35
    hosts = ("eutils.ncbi.nlm.nih.gov",)

    def __init__(self, config: object, *, resolve_secret: SecretResolver | None = None) -> None:
        """``config`` is ``Config.sources.pubmed``."""
        super().__init__(config, resolve_secret=resolve_secret)
        self.config = config if isinstance(config, PubmedConfig) else PubmedConfig()

    def key_refs(self) -> dict[str, str | None]:
        return {"api_key": self.config.api_key_ref, "email": self.config.email_ref}

    def _params(self, extra: dict[str, str]) -> dict[str, str]:
        params = {"db": "pubmed", **extra, "tool": self.config.tool}
        email = self.secret(self.config.email_ref, "email")
        if email:
            params["email"] = email
        key = self.secret(self.config.api_key_ref, "api_key")
        if key:
            params["api_key"] = key
        return params

    def _search(self, http: HttpClient, extra: dict[str, str]) -> list[str] | None:
        result = self.fetch(
            http,
            f"{EUTILS}/esearch.fcgi",
            params=self._params({**extra, "retmode": "json"}),
            kind="search",
        )
        if not result.ok:
            return None
        data = parse_json(result.body)
        ids = (data.get("esearchresult") or {}).get("idlist") if isinstance(data, dict) else None
        if not isinstance(ids, list):
            self.error("pubmed: unexpected esearch response")
            return None
        return [str(i) for i in ids]

    def _fetch_articles(self, http: HttpClient, ids: list[str], kind: str) -> list[ET.Element]:
        out: list[ET.Element] = []
        for group in chunked(ids, FETCH_BATCH):
            result = self.fetch(
                http,
                f"{EUTILS}/efetch.fcgi",
                params=self._params({"id": ",".join(group), "retmode": "xml"}),
                kind=kind,
            )
            if not result.ok:
                continue
            root = parse_xml(result.body)
            if root is None:
                self.error("pubmed: efetch response is not XML")
                continue
            out.extend(root.findall("PubmedArticle"))
        return out

    def discover(self, window: Window, profile: Profile, http: HttpClient) -> Iterator[Record]:
        mindate = window.start.astimezone(_dt.UTC).strftime("%Y/%m/%d")
        maxdate = window.end.astimezone(_dt.UTC).strftime("%Y/%m/%d")
        tags: dict[str, set[str]] = {}
        for channel_id, terms in core_terms(profile).items():
            for group in chunked(terms, TERMS_PER_QUERY):
                term = " OR ".join(f'"{t.replace(chr(34), "")}"[tiab]' for t in group)
                ids = self._search(
                    http,
                    {
                        "term": term,
                        "datetype": "edat",
                        "mindate": mindate,
                        "maxdate": maxdate,
                        "retmax": str(RETMAX),
                    },
                )
                for pmid in ids or []:
                    tags.setdefault(pmid, set()).add(f"pubmed:keywords:{channel_id}")
        if not tags:
            return
        for art in self._fetch_articles(http, list(tags), "list"):
            rec = parse_article(art)
            if rec is not None and rec.pubmed_id:
                rec.queries = tags.get(rec.pubmed_id, set())
                yield rec

    def _article_for(self, ident: Ident, http: HttpClient) -> ET.Element | None:
        if ident.kind is IdentKind.PUBMED:
            ids = [ident.value]
        elif ident.kind is IdentKind.DOI:
            found = self._search(http, {"term": f"{ident.value}[doi]", "retmax": "1"})
            if not found:
                return None
            ids = found[:1]
        else:
            return None
        arts = self._fetch_articles(http, ids, "single")
        return arts[0] if arts else None

    def metadata(self, ident: Ident, http: HttpClient) -> Record | None:
        art = self._article_for(ident, http)
        return parse_article(art) if art is not None else None

    def oa_pdf_candidates(self, record: Record, http: HttpClient) -> list[OaCandidate]:
        """PubMed Central landing page when the article has a PMC id (resolved by the archive)."""
        if not record.pubmed_id:
            return []
        art = self._article_for(Ident(IdentKind.PUBMED, record.pubmed_id), http)
        pmc = pmc_id(art) if art is not None else None
        if not pmc:
            return []
        return [
            OaCandidate(
                url=f"https://pmc.ncbi.nlm.nih.gov/articles/{pmc}/", source="pubmed", is_pdf=False
            )
        ]

    def test(self, http: HttpClient) -> SourceTest:
        def check(result: Fetch) -> str:
            data = parse_json(result.body)
            return "" if isinstance(data, dict) and "esearchresult" in data else "unexpected"

        params = self._params({"term": "science[tiab]", "retmax": "1", "retmode": "json"})
        out = self.timed_test(
            lambda: self.fetch(http, f"{EUTILS}/esearch.fcgi", params=params, kind="search"), check
        )
        out.key_present = "api_key" in params
        out.details["email_set"] = "email" in params
        return out
