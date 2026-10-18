from __future__ import annotations

import datetime as dt

from kallimachos.config.schema import IacrEprintConfig
from kallimachos.interfaces import Ident, IdentKind, Record, WorkKind
from kallimachos.sources.iacr_eprint import IacrEprintSource
from tests.unit.test_sources_fakes import Fail, FakeHttp, empty_profile, fixture, window

RSS = "https://eprint.iacr.org/rss/rss.xml"


def test_discover_filters_by_window() -> None:
    http = FakeHttp().add(RSS, fixture("iacr_eprint", "rss"))
    src = IacrEprintSource(IacrEprintConfig())
    recs = list(src.discover(window("2026-10-12T00:00", "2026-10-18T00:00"), empty_profile(), http))
    assert [r.eprint_id for r in recs] == ["2026/0001", "2026/0002"]
    first = recs[0]
    assert first.title == "A synthetic note on supersingular isogeny graphs"
    assert [a.name for a in first.authors] == ["Example Author A"]
    assert [a.name for a in recs[1].authors] == ["Example Author B", "Example Author C"]
    assert recs[1].title == "Toy lattice signatures & friends"
    assert first.published == dt.date(2026, 10, 17)
    assert first.kind is WorkKind.PREPRINT
    assert first.categories == ["Attacks and cryptanalysis"]
    assert first.url == "https://eprint.iacr.org/2026/0001"
    assert http.calls[0].params == {"order": "recent"}


def test_non_rss_is_a_source_error() -> None:
    page = fixture("iacr_eprint", "page")
    src = IacrEprintSource(IacrEprintConfig())
    w = window("2026-10-12T00:00", "2026-10-18T00:00")
    assert list(src.discover(w, empty_profile(), FakeHttp().add(RSS, page))) == []
    assert src.errors


def test_metadata_from_citation_meta() -> None:
    http = FakeHttp().add("https://eprint.iacr.org/2026/0001", fixture("iacr_eprint", "page"))
    rec = IacrEprintSource(IacrEprintConfig()).metadata(Ident(IdentKind.EPRINT, "2026/0001"), http)
    assert rec is not None
    assert rec.title == "A synthetic note on supersingular isogeny graphs"
    assert [a.name for a in rec.authors] == ["Example Author A", "Example Author B"]
    assert rec.published == dt.date(2026, 1, 1)
    assert rec.abstract and "isogeny graphs" in rec.abstract
    assert http.calls[0].kind == "single"
    assert IacrEprintSource(None).metadata(Ident(IdentKind.DOI, "10.0000/x"), http) is None


def test_oa_pdf_candidate() -> None:
    src = IacrEprintSource(IacrEprintConfig())
    cands = src.oa_pdf_candidates(Record(title="t", eprint_id="2026/0001"), FakeHttp())
    assert [(c.url, c.source) for c in cands] == [
        ("https://eprint.iacr.org/2026/0001.pdf", "eprint")
    ]
    assert src.oa_pdf_candidates(Record(title="t"), FakeHttp()) == []


def test_test() -> None:
    ok = IacrEprintSource(None).test(FakeHttp().add(RSS, fixture("iacr_eprint", "rss")))
    assert ok.ok
    bad = IacrEprintSource(None).test(FakeHttp().add(RSS, Fail(500)))
    assert not bad.ok and bad.http_status == 500
