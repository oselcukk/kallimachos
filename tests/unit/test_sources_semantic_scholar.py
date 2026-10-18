from __future__ import annotations

import datetime as dt
import json

from kallimachos.config.schema import SemanticScholarConfig
from kallimachos.interfaces import HttpResponse, Ident, IdentKind, Record, WorkKind
from kallimachos.sources.base import run_source
from kallimachos.sources.semantic_scholar import SemanticScholarSource
from tests.unit.test_sources_fakes import Fail, FakeHttp, example_profile, fixture, window

BULK = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
PAPER = "https://api.semanticscholar.org/graph/v1/paper/"
KEY = "fake-s2-key-0001"
W = window("2026-10-10T00:00", "2026-10-17T00:00")


def test_discover_parses_and_queries_by_channel() -> None:
    http = FakeHttp().add(BULK, fixture("semantic_scholar", "bulk"))
    recs = list(SemanticScholarSource(SemanticScholarConfig()).discover(W, example_profile(), http))
    assert len(recs) == 3
    first, second, third = recs
    assert first.s2_id == f"{1:040x}" and first.doi == "10.0000/example.31"
    assert first.kind is WorkKind.ARTICLE and first.published == dt.date(2026, 10, 16)
    assert [a.name for a in first.authors] == ["Example Author A", "Example Author B"]
    assert second.arxiv_id == "2610.00003" and second.kind is WorkKind.PREPRINT
    assert second.abstract is None
    assert third.published == dt.date(2026, 1, 1)  # year only
    assert len(http.calls) == 3  # one bulk query per channel
    call = http.calls[0]
    assert call.params["publicationDateOrYear"] == "2026-10-10:2026-10-16"
    assert call.params["query"].startswith('"riemann zeta function" | "dirichlet l-function"')
    assert call.kind == "search"


def test_token_continuation() -> None:
    page1 = json.loads(fixture("semantic_scholar", "bulk").body)
    page1["token"] = "NEXT"
    r1 = HttpResponse(url=BULK, status=200, headers={}, body=json.dumps(page1).encode())
    http = FakeHttp(repeat_last=False).add(BULK, r1, fixture("semantic_scholar", "bulk"))
    src = SemanticScholarSource(None)
    items = list(src._bulk(http, '"x"', "2026-10-10:2026-10-16"))
    assert len(items) == 6
    assert "token" not in http.calls[0].params and http.calls[1].params["token"] == "NEXT"


def test_429_closes_source_for_run() -> None:
    http = FakeHttp().add(BULK, Fail(429))
    src = SemanticScholarSource(SemanticScholarConfig())
    run = run_source(src, W, example_profile(), http)
    assert len(http.calls) == 1 and run.stats.count == 0
    assert any("closed for this run" in e for e in run.stats.errors)
    assert src.metadata(Ident(IdentKind.DOI, "10.0000/example.31"), http) is None
    assert len(http.calls) == 1


def test_key_only_in_x_api_key_header() -> None:
    http = FakeHttp().add(BULK, fixture("semantic_scholar", "bulk"))
    src = SemanticScholarSource(
        SemanticScholarConfig(api_key_ref="env:S2"), resolve_secret=lambda r: KEY
    )
    list(src.discover(W, example_profile(), http))
    for call in http.calls:
        assert call.headers == {"x-api-key": KEY}
        assert KEY not in call.url and KEY not in str(call.params)


def test_metadata_paths() -> None:
    http = FakeHttp().add(PAPER, fixture("semantic_scholar", "paper"))
    src = SemanticScholarSource(None)
    rec = src.metadata(Ident(IdentKind.DOI, "10.0000/example.31"), http)
    assert rec is not None and rec.title == "Fictional bounds for twisted exponential sums"
    src.metadata(Ident(IdentKind.ARXIV, "2610.00003"), http)
    src.metadata(Ident(IdentKind.PUBMED, "10000001"), http)
    assert [c.url for c in http.calls] == [
        f"{PAPER}DOI:10.0000/example.31",
        f"{PAPER}ARXIV:2610.00003",
        f"{PAPER}PMID:10000001",
    ]
    assert src.metadata(Ident(IdentKind.ZBMATH, "1"), http) is None


def test_oa_candidates() -> None:
    paper = json.loads(fixture("semantic_scholar", "bulk").body)["data"][1]
    resp = HttpResponse(url="x", status=200, headers={}, body=json.dumps(paper).encode())
    http = FakeHttp().add(PAPER, resp)
    cands = SemanticScholarSource(None).oa_pdf_candidates(
        Record(title="t", arxiv_id="2610.00003"), http
    )
    assert [(c.url, c.license) for c in cands] == [("https://arxiv.org/pdf/2610.00003", "CCBY")]
    closed = FakeHttp().add(PAPER, fixture("semantic_scholar", "paper"))
    assert (
        SemanticScholarSource(None).oa_pdf_candidates(Record(title="t", doi="10.0000/x"), closed)
        == []
    )


def test_test() -> None:
    search = "https://api.semanticscholar.org/graph/v1/paper/search"
    ok = SemanticScholarSource(None).test(
        FakeHttp().add(search, fixture("semantic_scholar", "bulk"))
    )
    assert ok.ok and ok.key_present is False
    bad = SemanticScholarSource(None).test(FakeHttp().add(search, Fail(429)))
    assert not bad.ok and bad.http_status == 429
