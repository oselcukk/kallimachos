"""Open-access PDF candidate order. Permanent: no other route.

1. arXiv PDF (versioned) - the arXiv source, or ``https://arxiv.org/pdf/<id><version>``;
2. IACR ePrint PDF - the ePrint source, or ``https://eprint.iacr.org/<id>.pdf``;
3. OpenAlex ``best_oa_location.pdf_url`` / ``open_access.oa_url`` / ``locations[is_oa].pdf_url``
   - proposed by the OpenAlex source (repository and author pages arrive here as OA locations);
4. Unpaywall OA locations, only when ``sources.unpaywall.email_ref`` is set.

Landing pages (``is_pdf=False``) are never scraped; they are reported as the ``landing`` of an
unreachable entry. There is no publisher, authenticated or shadow-library route.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from urllib.parse import quote

from kallimachos.archive.identity import journal_doi, split_arxiv_version
from kallimachos.config.schema import Config
from kallimachos.interfaces import HttpClient, OaCandidate, Record, Source

ORDER: tuple[str, ...] = ("arxiv", "iacr_eprint", "openalex", "unpaywall")
_SOURCE_ALIASES = {"eprint": "iacr_eprint", "iacr": "iacr_eprint"}
ARXIV_PDF_URL = "https://arxiv.org/pdf/{id}{version}"
EPRINT_PDF_URL = "https://eprint.iacr.org/{id}.pdf"
UNPAYWALL_URL = "https://api.unpaywall.org/v2/{doi}"

EmailResolver = Callable[[str], str]


def oa_candidates(
    record: Record,
    sources: Mapping[str, Source],
    http: HttpClient,
    config: Config,
    *,
    resolve_email: EmailResolver | None = None,
) -> list[OaCandidate]:
    """arXiv PDF (versioned) -> ePrint PDF -> OpenAlex best_oa_location/oa_url/locations[is_oa] ->
    Unpaywall
    (only when ``sources.unpaywall.email_ref`` is set)."""
    found: list[OaCandidate] = []
    if record.arxiv_id:
        found += _source_candidates(sources, "arxiv", record, http) or [_arxiv_pdf(record.arxiv_id)]
    if record.eprint_id:
        found += _source_candidates(sources, "iacr_eprint", record, http) or [
            OaCandidate(url=EPRINT_PDF_URL.format(id=record.eprint_id), source="iacr_eprint")
        ]
    found += _source_candidates(sources, "openalex", record, http)
    email_ref = config.sources.unpaywall.email_ref
    if email_ref and journal_doi(record):
        if "unpaywall" in sources:
            found += _source_candidates(sources, "unpaywall", record, http)
        elif resolve_email is not None:
            found += unpaywall_candidates(record, http, resolve_email(email_ref))
    return order_candidates(found)


def _arxiv_pdf(arxiv_id: str) -> OaCandidate:
    base, version = split_arxiv_version(arxiv_id)
    return OaCandidate(
        url=ARXIV_PDF_URL.format(id=base, version=version or ""), source="arxiv", version=version
    )


def _source_candidates(
    sources: Mapping[str, Source], name: str, record: Record, http: HttpClient
) -> list[OaCandidate]:
    source = sources.get(name)
    if source is None:
        return []
    try:
        return list(source.oa_pdf_candidates(record, http))
    except Exception:  # one failing source never stops the run
        return []


def source_rank(source: str) -> int:
    name = _SOURCE_ALIASES.get(source, source)
    return ORDER.index(name) if name in ORDER else len(ORDER)


def order_candidates(found: list[OaCandidate]) -> list[OaCandidate]:
    """Documented order, unknown routes dropped, duplicate URLs removed, landing pages last."""
    seen: set[str] = set()
    out: list[OaCandidate] = []
    for cand in sorted(found, key=lambda c: (not c.is_pdf, source_rank(c.source))):
        if source_rank(cand.source) >= len(ORDER):
            continue
        if not cand.url.startswith(("https://", "http://")) or cand.url in seen:
            continue
        seen.add(cand.url)
        out.append(cand)
    return out


def unpaywall_candidates(record: Record, http: HttpClient, email: str) -> list[OaCandidate]:
    """Unpaywall ``best_oa_location`` and ``oa_locations`` PDF URLs (archive only)."""
    doi = journal_doi(record)
    if not doi or not email:
        return []
    resp = http.get(
        UNPAYWALL_URL.format(doi=quote(doi, safe="/")), params={"email": email}, kind="single"
    )
    if resp is None or resp.status != 200:
        return []
    try:
        data = json.loads(resp.text())
    except ValueError:
        return []
    out: list[OaCandidate] = []
    locations = [data.get("best_oa_location") or {}, *(data.get("oa_locations") or [])]
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        if loc.get("url_for_pdf"):
            out.append(
                OaCandidate(url=loc["url_for_pdf"], source="unpaywall", license=loc.get("license"))
            )
        elif loc.get("url_for_landing_page"):
            out.append(
                OaCandidate(url=loc["url_for_landing_page"], source="unpaywall", is_pdf=False)
            )
    return out
