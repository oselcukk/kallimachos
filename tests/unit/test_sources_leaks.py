"""Key / e-mail / User-Agent leak test across all sources."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kallimachos.config.schema import (
    ArxivConfig,
    BiorxivConfig,
    CrossrefConfig,
    IacrEprintConfig,
    OpenAlexConfig,
    PubmedConfig,
    RssConfig,
    RssFeed,
    ScholarImapConfig,
    SemanticScholarConfig,
    ZbmathConfig,
)
from kallimachos.interfaces import Ident, IdentKind, Record, dump
from kallimachos.sources.arxiv import ArxivSource
from kallimachos.sources.base import BaseSource, run_source
from kallimachos.sources.biorxiv import BiorxivSource
from kallimachos.sources.crossref import CrossrefSource
from kallimachos.sources.iacr_eprint import IacrEprintSource
from kallimachos.sources.openalex import OpenAlexSource
from kallimachos.sources.pubmed import PubmedSource
from kallimachos.sources.rss import RssSource
from kallimachos.sources.scholar_imap import ScholarImapSource
from kallimachos.sources.semantic_scholar import SemanticScholarSource
from kallimachos.sources.zbmath import ZbmathSource
from tests.unit.test_sources_fakes import FakeHttp, example_profile, fixture, window
from tests.unit.test_sources_scholar_imap import FakeImap, _alert

KEY = "SENTINEL-KEY-7f3a9c"
EMAIL = "sentinel-mail@example.org"
SECRETS = {"env:KEY": KEY, "env:EMAIL": EMAIL, "env:USER": EMAIL, "env:PASS": KEY}
W = window("2026-10-10T00:00", "2026-10-17T00:00")

# where each secret may legitimately travel
ALLOWED_HEADER = {"openalex": "Authorization", "semantic_scholar": "x-api-key"}
ALLOWED_PARAM = {"pubmed": {"api_key", "email"}, "crossref": {"mailto"}}


def _resolve(ref: str) -> str:
    return SECRETS[ref]


def _cases() -> dict[str, tuple[Callable[[], BaseSource], Callable[[], FakeHttp]]]:
    oa = "https://api.openalex.org/works"
    s2 = "https://api.semanticscholar.org/graph/v1/paper"
    eutils = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    return {
        "arxiv": (
            lambda: ArxivSource(ArxivConfig(), resolve_secret=_resolve),
            lambda: FakeHttp().add("https://export.arxiv.org/", fixture("arxiv", "id_list")),
        ),
        "iacr_eprint": (
            lambda: IacrEprintSource(IacrEprintConfig(), resolve_secret=_resolve),
            lambda: (
                FakeHttp()
                .add("https://eprint.iacr.org/rss", fixture("iacr_eprint", "rss"))
                .add("https://eprint.iacr.org/2", fixture("iacr_eprint", "page"))
            ),
        ),
        "crossref": (
            lambda: CrossrefSource(CrossrefConfig(mailto_ref="env:EMAIL"), resolve_secret=_resolve),
            lambda: (
                FakeHttp()
                .add("https://api.crossref.org/journals", fixture("crossref", "journal_works"))
                .add("https://api.crossref.org/works", fixture("crossref", "work"))
            ),
        ),
        "openalex": (
            lambda: OpenAlexSource(OpenAlexConfig(api_key_ref="env:KEY"), resolve_secret=_resolve),
            lambda: (
                FakeHttp()
                .add(oa, fixture("openalex", "works_list"))
                .add(f"{oa}/", fixture("openalex", "work_single"))
            ),
        ),
        "semantic_scholar": (
            lambda: SemanticScholarSource(
                SemanticScholarConfig(api_key_ref="env:KEY"), resolve_secret=_resolve
            ),
            lambda: (
                FakeHttp()
                .add(f"{s2}/search", fixture("semantic_scholar", "bulk"))
                .add(f"{s2}/", fixture("semantic_scholar", "paper"))
            ),
        ),
        "zbmath": (
            lambda: ZbmathSource(ZbmathConfig(), resolve_secret=_resolve),
            lambda: (
                FakeHttp()
                .add("https://oai.zbmath.org/", fixture("zbmath", "oai_page2"))
                .add("https://api.zbmath.org/", fixture("zbmath", "rest_search"))
            ),
        ),
        "pubmed": (
            lambda: PubmedSource(
                PubmedConfig(api_key_ref="env:KEY", email_ref="env:EMAIL"), resolve_secret=_resolve
            ),
            lambda: (
                FakeHttp()
                .add(f"{eutils}esearch", fixture("pubmed", "esearch"))
                .add(f"{eutils}efetch", fixture("pubmed", "efetch"))
            ),
        ),
        "biorxiv": (
            lambda: BiorxivSource(BiorxivConfig(), resolve_secret=_resolve),
            lambda: FakeHttp().add("https://api.biorxiv.org/", fixture("biorxiv", "details")),
        ),
        "rss": (
            lambda: RssSource(
                RssConfig(feeds=[RssFeed(name="f", url="https://journal.example.org/search.rss")]),
                resolve_secret=_resolve,
            ),
            lambda: FakeHttp().add("https://journal.example.org/", fixture("rss", "rss2")),
        ),
        "scholar_imap": (
            lambda: ScholarImapSource(
                ScholarImapConfig(
                    host="imap.example.org", user_ref="env:USER", password_ref="env:PASS"
                ),
                resolve_secret=_resolve,
                imap_factory=lambda h, p: FakeImap([_alert()]),
            ),
            lambda: FakeHttp(),
        ),
    }


IDENTS = [
    Ident(IdentKind.ARXIV, "2610.00002"),
    Ident(IdentKind.DOI, "10.0000/example.11"),
    Ident(IdentKind.EPRINT, "2026/0001"),
    Ident(IdentKind.OPENALEX, "W1000000001"),
    Ident(IdentKind.PUBMED, "10000001"),
    Ident(IdentKind.ZBMATH, "9000001"),
]
RECORD = Record(
    title="t",
    doi="10.0000/example.11",
    arxiv_id="2610.00002",
    eprint_id="2026/0001",
    openalex_id="W1000000001",
    pubmed_id="10000001",
    s2_id="1",
    url="https://journal.example.org/a.pdf",
)


@pytest.mark.parametrize("name", list(_cases()))
def test_no_secret_or_user_agent_leaks(name: str) -> None:
    make_source, make_http = _cases()[name]
    http = make_http()
    src = make_source()
    run = run_source(src, W, example_profile(), http)
    outputs: list[Any] = [dump(r) for r in run.records]
    outputs += [run.stats.errors, run.warnings]
    for ident in IDENTS:
        rec = src.metadata(ident, http)
        if rec is not None:
            outputs.append(dump(rec))
    outputs += [dump(c) for c in src.oa_pdf_candidates(RECORD, http)]
    outputs.append(dump(src.test(http)))
    outputs += [src.errors, src.warnings]

    text = repr(outputs)
    assert KEY not in text and EMAIL not in text, "secret leaked into records/errors/results"
    for call in http.calls:
        assert KEY not in call.url and EMAIL not in call.url, "secret formatted into a URL"
        assert "User-Agent" not in call.headers and "user-agent" not in call.headers
        for header, value in call.headers.items():
            if KEY in value or EMAIL in value:
                assert ALLOWED_HEADER.get(name) == header, f"{name} sent a secret in {header}"
        for param, value in call.params.items():
            if KEY in value or EMAIL in value:
                assert param in ALLOWED_PARAM.get(name, set()), f"{name} sent a secret in {param}"
    if name == "openalex":
        assert all(c.headers.get("Authorization") == f"Bearer {KEY}" for c in http.calls)
        assert all("api_key" not in c.params for c in http.calls)
    if name == "semantic_scholar":
        assert all(c.headers.get("x-api-key") == KEY for c in http.calls)
    if name == "crossref":
        assert all(c.params.get("mailto") == EMAIL for c in http.calls)


def test_mailto_is_crossref_only() -> None:
    for name, (make_source, make_http) in _cases().items():
        if name == "crossref" or name == "pubmed":
            continue
        http = make_http()
        list(run_source(make_source(), W, example_profile(), http).records)
        assert all("mailto" not in c.params for c in http.calls), name
