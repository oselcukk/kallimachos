"""zbMATH Open source.

OAI-PMH ListRecords per MSC set (template configurable) and REST document/_search; 400
noRecordsMatch silent; one restart on badResumptionToken.
Fixtures: ``tests/fixtures/zbmath/``.

Observed OAI behaviour (unverified by documentation):
an empty set answers HTTP 400 with ``noRecordsMatch`` (not an error); a ``resumptionToken`` works
once, a repeated or lost page answers HTTP 404 ``badResumptionToken`` -> the set is fetched again
from the start once (records are de-duplicated by OAI identifier), a second loss is an error. An
answer that is neither ``ListRecords`` nor an OAI error is a source error; the run goes on.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Iterator
from typing import Any
from xml.etree import ElementTree as ET

from kallimachos.config.schema import ZbmathConfig
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
    find_arxiv,
    normalize_doi,
    parse_iso_date,
    parse_json,
    parse_xml,
)

OAI_URL = "https://oai.zbmath.org/v1/"
REST_URL = "https://api.zbmath.org/v1/document/_search"
NS = {"o": "http://www.openarchives.org/OAI/2.0/", "z": "https://zbmath.org/zbmath/elements/1.0/"}
UNAVAILABLE_MARK = "unavailable due to conflicting licenses"
DOC_TYPE_KIND: dict[str, WorkKind] = {
    "j": WorkKind.ARTICLE,
    "b": WorkKind.BOOK,
    "a": WorkKind.CHAPTER,
}


def zb_name(raw: str) -> str:
    """``"Last, First"`` -> ``"First Last"``."""
    raw = clean_text(raw)
    if "," in raw:
        last, first = raw.split(",", 1)
        return clean_text(f"{first} {last}")
    return raw


def oai_error(root: ET.Element) -> str | None:
    """OAI error name. zbMATH puts the HTTP code in ``code`` and the name in the text; standard
    servers put the name in ``code``. Both are accepted."""
    err = root.find("o:error", NS)
    if err is None:
        return None
    code = (err.get("code") or "").strip()
    text = (err.text or "").strip()
    if code and not code.isdigit():
        return code
    return text.split()[0] if text else (code or "error")


def review_text(value: str) -> str | None:
    text = clean_text(value)
    if not text or text == "None" or UNAVAILABLE_MARK in text:
        return None
    return text


def parse_oai_record(rec: ET.Element) -> Record | None:
    md = rec.find("o:metadata", NS)
    if md is None:
        return None
    z = next(iter(md), None)
    if z is None:
        return None

    def g(tag: str) -> str:
        return clean_text(z.findtext(f"z:{tag}", "", NS))

    zid = g("document_id")
    title = g("document_title")
    if not zid or not title:
        return None
    links = [c.text.strip() for c in z.findall(".//z:link", NS) if c.text]
    arxiv = find_arxiv(*links)
    doi = normalize_doi(g("doi")) if g("doi") != "None" else None
    authors = [zb_name(a) for a in g("author").split(";") if a.strip() and a.strip() != "None"]
    abstract = review_text(z.findtext("z:review/z:review_text", "", NS) or "")
    serial = clean_text(z.findtext("z:serial/z:serial_title", "", NS))
    return Record(
        title=title,
        authors=[Author(name=a) for a in authors if a],
        abstract=abstract,
        abstract_source="zbmath" if abstract else None,
        venue=serial or g("source") or None,
        published=parse_iso_date(g("publication_year")),
        created=parse_iso_date(g("time")),
        doi=doi,
        arxiv_id=arxiv[0] if arxiv else None,
        zbmath_id=zid,
        url=f"https://doi.org/{doi}" if doi else f"https://zbmath.org/{zid}",
        msc=[c.text.strip() for c in z.findall("z:classifications/z:classification", NS) if c.text],
        keywords=[clean_text(k.text) for k in z.findall("z:keywords/z:keyword", NS) if k.text],
        kind=DOC_TYPE_KIND.get(g("document_type"), WorkKind.UNKNOWN),
        sources={"zbmath"},
    )


def parse_rest_document(d: dict[str, Any]) -> Record | None:
    title = clean_text((d.get("title") or {}).get("title"))
    zid = d.get("id")
    if not title or zid is None:
        return None
    links = d.get("links") or []
    doi = next((normalize_doi(x.get("identifier")) for x in links if x.get("type") == "doi"), None)
    arxiv = find_arxiv(*(str(x.get("url") or "") for x in links))
    contributors = (d.get("contributors") or {}).get("authors") or []
    review = next(
        (review_text(str(x.get("text") or "")) for x in d.get("editorial_contributions") or []),
        None,
    )
    series = ((d.get("source") or {}).get("series") or [{}])[0] or {}
    return Record(
        title=title,
        authors=[Author(name=zb_name(a.get("name", ""))) for a in contributors if a.get("name")],
        abstract=review,
        abstract_source="zbmath" if review else None,
        venue=clean_text(series.get("title")) or None,
        issn=[str(i.get("number")) for i in series.get("issn") or [] if i.get("number")],
        published=parse_iso_date(str(d.get("year") or "")),
        created=parse_iso_date(d.get("datestamp")),
        doi=doi,
        arxiv_id=arxiv[0] if arxiv else None,
        zbmath_id=str(zid),
        url=f"https://doi.org/{doi}" if doi else str(d.get("zbmath_url") or ""),
        msc=[str(m.get("code")) for m in d.get("msc") or [] if m.get("code")],
        keywords=[clean_text(k) for k in d.get("keywords") or [] if k],
        kind=DOC_TYPE_KIND.get(
            str((d.get("document_type") or {}).get("code") or ""), WorkKind.UNKNOWN
        ),
        sources={"zbmath"},
    )


def msc_prefixes(profile: Profile) -> list[str]:
    """Profile MSC codes as match prefixes (``11Fxx`` -> ``11F``)."""
    out: list[str] = []
    for ch in profile.channels:
        for code in [*ch.msc.primary, *ch.msc.secondary]:
            p = re.sub(r"[xX\-]+$", "", code.strip())
            if p and p not in out:
                out.append(p)
    return out


class ZbmathSource(BaseSource):
    name = "zbmath"
    needs_key = False
    default_interval_s = 2.5
    hosts = ("oai.zbmath.org", "api.zbmath.org")

    def __init__(self, config: object, *, resolve_secret: SecretResolver | None = None) -> None:
        """``config`` is ``Config.sources.zbmath``."""
        super().__init__(config, resolve_secret=resolve_secret)
        self.config = config if isinstance(config, ZbmathConfig) else ZbmathConfig()

    def sets(self, profile: Profile) -> list[str]:
        out: list[str] = []
        for prefix in msc_prefixes(profile):
            msc2 = prefix[:2]
            if len(msc2) == 2 and msc2.isdigit():
                name = self.config.oai_set_template.format(msc2=msc2)
                if name not in out:
                    out.append(name)
        return out

    def discover(self, window: Window, profile: Profile, http: HttpClient) -> Iterator[Record]:
        prefixes = msc_prefixes(profile)
        oldest = window.end.year - self.config.oldest_year_delta
        since = window.start.astimezone(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        seen: set[str] = set()
        for set_name in self.sets(profile):
            for rec in self._list_set(http, set_name, since):
                if rec.zbmath_id in seen:
                    continue
                seen.add(rec.zbmath_id or "")
                if not any(code.startswith(p) for code in rec.msc for p in prefixes):
                    continue
                if rec.published is not None and rec.published.year < oldest:
                    continue
                rec.queries = {f"zbmath:oai:{set_name}"}
                yield rec

    def _list_set(self, http: HttpClient, set_name: str, since: str) -> Iterator[Record]:
        first = {
            "verb": "ListRecords",
            "metadataPrefix": "oai_zb_preview",
            "set": set_name,
            "from": since,
        }
        params = dict(first)
        page = 0
        restarts = 0
        identifiers: set[str] = set()
        while page < self.config.max_pages_per_set:
            # HTTP 400 carries the OAI error body (``noRecordsMatch`` / ``badResumptionToken``)
            result = self.fetch(
                http,
                OAI_URL,
                params=params,
                timeout=float(self.config.timeout_s),
                accept_status=(400,),
            )
            root = parse_xml(result.body) if result.body else None
            error = oai_error(root) if root is not None else None
            if error == "noRecordsMatch" or (
                not result.ok and result.status == 400 and "resumptionToken" not in params
            ):
                self.silence(result)
                return
            if not result.ok or error == "badResumptionToken":
                if "resumptionToken" in params and restarts < 1:
                    restarts += 1
                    self.silence(result)
                    self.warn(f"zbmath: set {set_name} page {page + 1} lost; set restarted once")
                    params, page = dict(first), 0
                    continue
                reason = error or (f"HTTP {result.status}" if result.status else "no response")
                self.error(f"zbmath: set {set_name} page {page + 1}: {reason}")
                return
            if root is None:
                self.error(f"zbmath: set {set_name}: response is not XML")
                return
            if error is not None:
                self.error(f"zbmath: set {set_name}: OAI error {error}")
                return
            listing = root.find("o:ListRecords", NS)
            if listing is None:
                self.error(f"zbmath: set {set_name}: unexpected OAI response")
                return
            for rec_el in listing.findall("o:record", NS):
                ident = rec_el.findtext("o:header/o:identifier", "", NS)
                if ident in identifiers:
                    continue
                identifiers.add(ident)
                rec = parse_oai_record(rec_el)
                if rec is not None:
                    yield rec
            token = (listing.findtext("o:resumptionToken", "", NS) or "").strip()
            if not token:
                return
            params = {"verb": "ListRecords", "resumptionToken": token}
            page += 1

    def metadata(self, ident: Ident, http: HttpClient) -> Record | None:
        if ident.kind is not IdentKind.ZBMATH:
            return None
        result = self.fetch(
            http, REST_URL, params={"search_string": f"an:{ident.value}"}, kind="single"
        )
        if not result.ok:
            return None
        data = parse_json(result.body)
        docs = data.get("result") if isinstance(data, dict) else None
        if not isinstance(docs, list):
            return None
        for d in docs:
            if isinstance(d, dict) and str(d.get("id")) == ident.value:
                return parse_rest_document(d)
        return parse_rest_document(docs[0]) if docs and isinstance(docs[0], dict) else None

    def oa_pdf_candidates(self, record: Record, http: HttpClient) -> list[OaCandidate]:
        """zbMATH hosts no full texts; arXiv links are proposed by the arXiv source."""
        return []

    def test(self, http: HttpClient) -> SourceTest:
        def check(result: Fetch) -> str:
            root = parse_xml(result.body)
            ok = root is not None and root.find("o:Identify", NS) is not None
            return "" if ok else "unexpected OAI Identify response"

        return self.timed_test(
            lambda: self.fetch(http, OAI_URL, params={"verb": "Identify"}, kind="single"), check
        )
