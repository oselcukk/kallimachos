from __future__ import annotations

import datetime as dt

from kallimachos.config.schema import ArxivConfig
from kallimachos.interfaces import Ident, IdentKind, Record, WorkKind
from kallimachos.sources.arxiv import ArxivSource, canonical_category
from tests.unit.test_sources_fakes import Fail, FakeHttp, example_profile, fixture, window

API = "https://export.arxiv.org/api/query"
HTTP_API = "http://export.arxiv.org/api/query"


def test_discover_parses_fixture_and_builds_or_query() -> None:
    http = FakeHttp().add(API, fixture("arxiv", "list"))
    src = ArxivSource(ArxivConfig())
    recs = list(
        src.discover(window("2026-10-13T00:00", "2026-10-17T00:00"), example_profile(), http)
    )
    assert [r.arxiv_id for r in recs] == ["2610.00001", "2610.00002", "2610.00003"]
    first = recs[0]
    assert first.title == "Fictional bounds for twisted exponential sums"
    assert first.authors[0].name == "Example Author A"
    assert first.kind is WorkKind.PREPRINT
    assert first.published == dt.date(2026, 10, 15)
    assert first.url == "https://arxiv.org/abs/2610.00001v1"
    assert first.abstract and first.abstract_source == "arxiv"
    second = recs[1]
    assert second.doi == "10.0000/example.1"
    assert second.venue == "Example J. Math. 1 (2026) 1-12"
    assert second.primary_category == "math.NT"
    assert "math.MP" in second.categories
    q = http.calls[0].params["search_query"]
    assert q.startswith("(cat:math.NT OR cat:math.CA OR cat:math.PR OR cat:math.AG OR cat:cs.CR")
    assert "submittedDate:[202610130000 TO 202610170000]" in q
    assert http.calls[0].params["start"] == "0"


def test_window_is_rendered_in_gmt() -> None:
    http = FakeHttp().add(API, fixture("arxiv", "empty"))
    tz = dt.timezone(dt.timedelta(hours=3))
    w = window("2026-10-13T00:00", "2026-10-17T00:00")
    w = type(w)(start=w.start.astimezone(tz), end=w.end.astimezone(tz))
    list(ArxivSource(ArxivConfig()).discover(w, example_profile(), http))
    assert "submittedDate:[202610130000 TO 202610170000]" in http.calls[0].params["search_query"]


def test_alias_map() -> None:
    assert canonical_category("math.MP") == "math-ph"
    assert canonical_category("math.IT") == "cs.IT"
    assert canonical_category("math.NT") == "math.NT"
    prof = example_profile()
    prof.channels[0].arxiv.secondary = ["math.MP", "math.IT"]
    cats = ArxivSource(ArxivConfig()).categories(prof)
    assert "math-ph" in cats and "cs.IT" in cats and "math.MP" not in cats


def test_paging_limits_respect_30000() -> None:
    page = fixture("arxiv", "list")
    body = page.body.replace(b"<opensearch:totalResults>3<", b"<opensearch:totalResults>99999<")
    page = type(page)(url=page.url, status=200, headers={}, body=body)
    http = FakeHttp().add(API, page)
    cfg = ArxivConfig(page_size=5000, max_records=40000)
    src = ArxivSource(cfg)
    list(src._pages(http, "cat:math.NT"))
    starts = [int(c.params["start"]) for c in http.calls]
    sizes = [int(c.params["max_results"]) for c in http.calls]
    assert max(sizes) <= 2000
    assert all(s + n <= 30000 for s, n in zip(starts, sizes, strict=True))
    assert starts[-1] + sizes[-1] <= 30000
    assert len(http.calls) == 10000  # 3 records per page up to the 30000 offset


def test_max_records_stops_paging() -> None:
    page = fixture("arxiv", "list")
    body = page.body.replace(b"<opensearch:totalResults>3<", b"<opensearch:totalResults>500<")
    http = FakeHttp().add(API, type(page)(url=page.url, status=200, headers={}, body=body))
    src = ArxivSource(ArxivConfig(page_size=3, max_records=6))
    recs = list(src._pages(http, "cat:math.NT"))
    assert len(http.calls) == 2 and len(recs) == 6


def test_https_failure_falls_back_to_http_and_sticks() -> None:
    http = FakeHttp().add(API, Fail(None)).add(HTTP_API, fixture("arxiv", "list"))
    src = ArxivSource(ArxivConfig())
    recs = list(
        src.discover(window("2026-10-13T00:00", "2026-10-17T00:00"), example_profile(), http)
    )
    assert len(recs) == 3
    assert [c.url for c in http.calls] == [API, HTTP_API]
    assert any("using http://" in w for w in src.warnings)
    src.metadata(Ident(IdentKind.ARXIV, "2610.00002"), http)
    assert http.calls[-1].url == HTTP_API


def test_client_error_does_not_fall_back() -> None:
    http = FakeHttp().add(API, Fail(400))
    src = ArxivSource(ArxivConfig())
    assert list(src._pages(http, "cat:math.NT")) == []
    assert len(http.calls) == 1


def test_empty_page_is_retried_once() -> None:
    page = fixture("arxiv", "empty")
    body = page.body.replace(b"<opensearch:totalResults>0<", b"<opensearch:totalResults>5<")
    http = FakeHttp().add(API, type(page)(url=page.url, status=200, headers={}, body=body))
    assert list(ArxivSource(ArxivConfig())._pages(http, "cat:math.NT")) == []
    assert len(http.calls) == 2


def test_no_categories_warns() -> None:
    http = FakeHttp()
    src = ArxivSource(ArxivConfig())
    prof = example_profile()
    for ch in prof.channels:
        ch.arxiv.primary, ch.arxiv.secondary = [], []
    assert list(src.discover(window("2026-10-13T00:00", "2026-10-17T00:00"), prof, http)) == []
    assert src.warnings and not http.calls


def test_metadata_by_id_and_arxiv_doi() -> None:
    http = FakeHttp().add(API, fixture("arxiv", "id_list"))
    src = ArxivSource(ArxivConfig())
    rec = src.metadata(Ident(IdentKind.ARXIV, "2610.00002"), http)
    assert rec is not None and rec.arxiv_id == "2610.00002"
    assert http.calls[0].params["id_list"] == "2610.00002"
    assert http.calls[0].kind == "single"
    rec2 = src.metadata(Ident(IdentKind.DOI, "10.48550/arXiv.2610.00002"), http)
    assert rec2 is not None and http.calls[1].params["id_list"] == "2610.00002"
    assert src.metadata(Ident(IdentKind.DOI, "10.0000/example.1"), http) is None


def test_oa_pdf_is_versioned() -> None:
    src = ArxivSource(ArxivConfig())
    rec = Record(title="t", arxiv_id="2610.00002", url="https://arxiv.org/abs/2610.00002v2")
    cands = src.oa_pdf_candidates(rec, FakeHttp())
    assert [(c.url, c.version, c.source) for c in cands] == [
        ("https://arxiv.org/pdf/2610.00002v2", "v2", "arxiv")
    ]
    # no version known -> one id_list lookup for the current version
    http = FakeHttp().add(API, fixture("arxiv", "id_list"))
    cands = src.oa_pdf_candidates(Record(title="t", arxiv_id="2610.00002"), http)
    assert cands[0].url == "https://arxiv.org/pdf/2610.00002v2"
    assert src.oa_pdf_candidates(Record(title="no id"), FakeHttp()) == []


def test_test_one_request() -> None:
    http = FakeHttp().add(API, fixture("arxiv", "empty"))
    res = ArxivSource(ArxivConfig()).test(http)
    assert res.ok and res.source == "arxiv" and res.http_status == 200
    assert len(http.calls) == 1
    bad = ArxivSource(ArxivConfig()).test(FakeHttp().add(API, Fail(503)).add(HTTP_API, Fail(503)))
    assert not bad.ok and bad.http_status == 503
