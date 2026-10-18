from __future__ import annotations

import datetime as dt

from kallimachos.config.schema import PubmedConfig
from kallimachos.interfaces import Ident, IdentKind, Record, WorkKind
from kallimachos.sources.pubmed import PubmedSource
from tests.unit.test_sources_fakes import Fail, FakeHttp, example_profile, fixture, window

ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
W = window("2026-10-16T00:00", "2026-10-18T00:00")


def _http() -> FakeHttp:
    return (
        FakeHttp()
        .add(ESEARCH, fixture("pubmed", "esearch"))
        .add(EFETCH, fixture("pubmed", "efetch"))
    )


def test_discover_esearch_then_efetch() -> None:
    http = _http()
    recs = list(PubmedSource(PubmedConfig()).discover(W, example_profile(), http))
    assert [r.pubmed_id for r in recs] == ["10000001", "10000002"]
    first = recs[0]
    assert first.title == "Fictional bounds for twisted exponential sums"
    assert first.doi == "10.0000/example.51"
    assert [a.name for a in first.authors] == ["Example Author A", "Example Author B"]
    assert first.venue == "Example journal of medicine"
    assert first.published == dt.date(2026, 9, 11)
    assert first.created == dt.date(2026, 10, 17)
    assert first.kind is WorkKind.ARTICLE and first.abstract_source == "pubmed"
    assert recs[1].abstract is None
    assert {"pubmed:keywords:A", "pubmed:keywords:B"} <= first.queries
    searches = [c for c in http.calls if c.url == ESEARCH]
    assert len(searches) == 3
    p = searches[0].params
    assert p["db"] == "pubmed" and p["datetype"] == "edat"
    assert p["mindate"] == "2026/10/16" and p["maxdate"] == "2026/10/18"
    assert p["term"].startswith('"riemann zeta function"[tiab] OR')
    assert p["tool"] == "kallimachos" and "email" not in p and "api_key" not in p
    fetches = [c for c in http.calls if c.url == EFETCH]
    assert len(fetches) == 1 and fetches[0].params["id"] == "10000001,10000002"


def test_tool_email_and_key_go_in_params() -> None:
    secrets = {"env:PM_EMAIL": "reader@example.org", "env:PM_KEY": "fake-pubmed-key-0001"}
    http = _http()
    src = PubmedSource(
        PubmedConfig(email_ref="env:PM_EMAIL", api_key_ref="env:PM_KEY", tool="kallimachos"),
        resolve_secret=secrets.__getitem__,
    )
    list(src.discover(W, example_profile(), http))
    for call in http.calls:
        assert call.params["tool"] == "kallimachos"
        assert call.params["email"] == "reader@example.org"
        assert call.params["api_key"] == "fake-pubmed-key-0001"
        assert "fake-pubmed-key-0001" not in call.url and not call.headers


def test_metadata_by_pmid_and_doi() -> None:
    http = _http()
    src = PubmedSource(None)
    rec = src.metadata(Ident(IdentKind.PUBMED, "10000001"), http)
    assert rec is not None and rec.pubmed_id == "10000001"
    assert http.calls[0].url == EFETCH and http.calls[0].kind == "single"
    rec2 = src.metadata(Ident(IdentKind.DOI, "10.0000/example.51"), http)
    assert rec2 is not None and http.calls[1].params["term"] == "10.0000/example.51[doi]"
    assert src.metadata(Ident(IdentKind.ARXIV, "2610.00001"), http) is None


def test_oa_candidate_is_pmc_landing_page() -> None:
    cands = PubmedSource(None).oa_pdf_candidates(Record(title="t", pubmed_id="10000001"), _http())
    assert [(c.url, c.is_pdf) for c in cands] == [
        ("https://pmc.ncbi.nlm.nih.gov/articles/PMC0000001/", False)
    ]
    assert PubmedSource(None).oa_pdf_candidates(Record(title="t"), FakeHttp()) == []


def test_test() -> None:
    res = PubmedSource(None).test(FakeHttp().add(ESEARCH, fixture("pubmed", "esearch")))
    assert res.ok and res.key_present is False
    assert not PubmedSource(None).test(FakeHttp().add(ESEARCH, Fail(500))).ok
