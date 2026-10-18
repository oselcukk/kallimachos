from __future__ import annotations

import datetime as dt
import json

from kallimachos.config.schema import BiorxivConfig
from kallimachos.interfaces import HttpResponse, Ident, IdentKind, Record, WorkKind
from kallimachos.sources.biorxiv import BiorxivSource, bx_name
from tests.unit.test_sources_fakes import Fail, FakeHttp, empty_profile, fixture, window

API = "https://api.biorxiv.org/details/"
W = window("2026-10-14T00:00", "2026-10-16T00:00")


def _page(n: int, total: int, start: int) -> HttpResponse:
    data = json.loads(fixture("biorxiv", "details").body)
    item = data["collection"][0]
    data["collection"] = [{**item, "doi": f"10.0000/2026.10.07.{start + i:06d}"} for i in range(n)]
    data["messages"][0]["total"] = str(total)
    return HttpResponse(url="x", status=200, headers={}, body=json.dumps(data).encode())


def test_discover_parses_fixture() -> None:
    http = FakeHttp().add(API, fixture("biorxiv", "details"))
    recs = list(BiorxivSource(BiorxivConfig()).discover(W, empty_profile(), http))
    assert len(recs) == 3
    first = recs[0]
    assert first.title == "Toy models of modular forms over finite fields"
    assert [a.name for a in first.authors] == ["E. A. Author", "E. B. Author"]
    assert first.doi == "10.0000/2026.10.07.000001"
    assert first.url == "https://www.biorxiv.org/content/10.0000/2026.10.07.000001v1"
    assert first.published == dt.date(2026, 10, 14)
    assert first.kind is WorkKind.PREPRINT and first.venue == "bioRxiv"
    assert first.categories == ["bioinformatics"]
    assert http.calls[0].url == f"{API}biorxiv/2026-10-14/2026-10-15/0/json"


def test_cursor_paging_by_30() -> None:
    http = FakeHttp(repeat_last=False).add(
        API, _page(30, 65, 0), _page(30, 65, 30), _page(5, 65, 60)
    )
    recs = list(BiorxivSource(BiorxivConfig()).discover(W, empty_profile(), http))
    assert len(recs) == 65
    assert [c.url.rsplit("/", 2)[-2] for c in http.calls] == ["0", "30", "60"]


def test_medrxiv_server_and_newest_version() -> None:
    data = json.loads(fixture("biorxiv", "doi").body)
    data["messages"][0]["total"] = "2"
    page = HttpResponse(url="x", status=200, headers={}, body=json.dumps(data).encode())
    http = FakeHttp().add(API, page)
    recs = list(
        BiorxivSource(BiorxivConfig(servers=["medrxiv"])).discover(W, empty_profile(), http)
    )
    assert len(recs) == 1 and recs[0].url and recs[0].url.endswith("v2")
    assert recs[0].url.startswith("https://www.medrxiv.org/") and recs[0].venue == "medRxiv"
    assert "/medrxiv/" in http.calls[0].url


def test_metadata_newest_version() -> None:
    http = FakeHttp().add(API, fixture("biorxiv", "doi"))
    rec = BiorxivSource(None).metadata(Ident(IdentKind.DOI, "10.0000/2026.10.07.000002"), http)
    assert rec is not None and rec.url and rec.url.endswith("000002v2")
    assert http.calls[0].url == f"{API}biorxiv/10.0000/2026.10.07.000002/na/json"
    assert BiorxivSource(None).metadata(Ident(IdentKind.ARXIV, "2610.00001"), http) is None


def test_oa_candidate_full_pdf() -> None:
    src = BiorxivSource(None)
    rec = Record(
        title="t",
        doi="10.0000/2026.10.07.000001",
        url="https://www.biorxiv.org/content/10.0000/2026.10.07.000001v1",
    )
    cands = src.oa_pdf_candidates(rec, FakeHttp())
    assert [(c.url, c.version) for c in cands] == [
        ("https://www.biorxiv.org/content/10.0000/2026.10.07.000001v1.full.pdf", "v1")
    ]
    http = FakeHttp().add(API, fixture("biorxiv", "doi"))
    cands = src.oa_pdf_candidates(Record(title="t", doi="10.0000/2026.10.07.000002"), http)
    assert cands[0].version == "v2"


def test_test() -> None:
    assert BiorxivSource(None).test(FakeHttp().add(API, fixture("biorxiv", "details"))).ok
    assert not BiorxivSource(None).test(FakeHttp().add(API, Fail(None))).ok


def test_name_order() -> None:
    assert bx_name("Author, E. A.") == "E. A. Author"
