from __future__ import annotations

import datetime as dt

from kallimachos.config.schema import ZbmathConfig
from kallimachos.interfaces import HttpResponse, Ident, IdentKind, Record, WorkKind
from kallimachos.sources.base import run_source
from kallimachos.sources.zbmath import ZbmathSource, msc_prefixes, zb_name
from tests.unit.test_sources_fakes import Fail, FakeHttp, example_profile, fixture, window

OAI = "https://oai.zbmath.org/v1/"
REST = "https://api.zbmath.org/v1/document/_search"
W = window("2026-10-10T00:00", "2026-10-17T00:00")


def _http(*replies: HttpResponse | Fail) -> FakeHttp:
    return FakeHttp(repeat_last=False).add(OAI, *replies)


def _single_set_profile():  # type: ignore[no-untyped-def]
    prof = example_profile()
    prof.channels = prof.channels[1:2]  # channel B: MSC 11G05, 11G10, 11F11, 14G05
    return prof


def test_discover_pages_with_resumption_token() -> None:
    prof = _single_set_profile()
    http = _http(
        fixture("zbmath", "oai_page1"),
        fixture("zbmath", "oai_page2"),
        fixture("zbmath", "oai_no_records"),  # set 14
    )
    src = ZbmathSource(ZbmathConfig())
    run = run_source(src, W, prof, http)
    assert src.sets(prof) == ["11", "14"]
    first = http.calls[0].params
    assert first == {
        "verb": "ListRecords",
        "metadataPrefix": "oai_zb_preview",
        "set": "11",
        "from": "2026-10-10T00:00:00Z",
    }
    assert http.calls[1].params == {"verb": "ListRecords", "resumptionToken": "example-token-1"}
    # 9000001 has MSC 11D61/11B39 (no channel match) and 9000003 is too old: only 9000002 stays
    assert [r.zbmath_id for r in run.records] == ["9000002"]
    rec = run.records[0]
    assert rec.title == "Toy models of modular forms over finite fields"
    assert rec.authors[0].name == "Example C Author"
    assert rec.msc == ["11F11", "14G35"]
    assert rec.abstract and rec.abstract_source == "zbmath"
    assert rec.kind is WorkKind.ARTICLE and rec.published == dt.date(2026, 1, 1)
    assert run.stats.errors == []  # noRecordsMatch is silent


def test_unavailable_review_and_arxiv_link() -> None:
    prof = example_profile()
    prof.channels[0].msc.primary = ["11D61"]
    http = _http(fixture("zbmath", "oai_page1"), fixture("zbmath", "oai_page2")).add(
        OAI, fixture("zbmath", "oai_no_records")
    )
    http.repeat_last = True
    recs = [r for r in ZbmathSource(None).discover(W, prof, http) if r.zbmath_id == "9000001"]
    assert recs and recs[0].abstract is None  # licence placeholder is not an abstract
    assert recs[0].arxiv_id == "2610.00004" and recs[0].doi == "10.0000/example.41"


def test_http_400_without_body_is_silent() -> None:
    http = _http(Fail(400), Fail(400))
    run = run_source(ZbmathSource(None), W, _single_set_profile(), http)
    assert run.records == [] and run.stats.errors == []
    assert http.errors == []  # accepted status (accept_status=(400,)): no error record either
    assert all(c.kind == "list" for c in http.calls) and len(http.calls) == 2


def test_bad_resumption_token_restarts_set_once() -> None:
    prof = _single_set_profile()
    http = _http(
        fixture("zbmath", "oai_page1"),
        fixture("zbmath", "oai_bad_token"),
        fixture("zbmath", "oai_page1"),
        fixture("zbmath", "oai_page2"),
        fixture("zbmath", "oai_no_records"),
    )
    src = ZbmathSource(None)
    run = run_source(src, W, prof, http)
    assert [r.zbmath_id for r in run.records] == ["9000002"]
    assert http.calls[2].params["set"] == "11"  # restarted from the beginning
    assert run.stats.errors == []
    assert any("restarted once" in w for w in run.warnings)


def test_second_token_loss_is_an_error() -> None:
    http = _http(
        fixture("zbmath", "oai_page1"),
        Fail(404),
        fixture("zbmath", "oai_page1"),
        Fail(404),
        fixture("zbmath", "oai_no_records"),
    )
    run = run_source(ZbmathSource(None), W, _single_set_profile(), http)
    assert any("set 11 page 2" in e for e in run.stats.errors)
    assert len(http.calls) == 5  # the next set still runs


def test_unexpected_response_is_source_error() -> None:
    junk = HttpResponse(url=OAI, status=200, headers={}, body=b"<html>maintenance</html>")
    http = _http(junk, fixture("zbmath", "oai_no_records"))
    run = run_source(ZbmathSource(None), W, _single_set_profile(), http)
    assert any("unexpected OAI response" in e for e in run.stats.errors)


def test_set_template_is_configurable() -> None:
    src = ZbmathSource(ZbmathConfig(oai_set_template="msc:{msc2}"))
    assert src.sets(_single_set_profile()) == ["msc:11", "msc:14"]


def test_helpers() -> None:
    assert zb_name("Author, Example A") == "Example A Author"
    prof = example_profile()
    prof.channels[0].msc.primary = ["11Mxx", "11N-"]
    assert msc_prefixes(prof)[:2] == ["11M", "11N"]


def test_metadata_rest() -> None:
    http = FakeHttp().add(REST, fixture("zbmath", "rest_search"))
    rec = ZbmathSource(None).metadata(Ident(IdentKind.ZBMATH, "9000001"), http)
    assert rec is not None
    assert rec.title == "An invented family of Diophantine equations"
    assert rec.doi == "10.0000/example.41" and rec.arxiv_id == "2610.00004"
    assert [a.name for a in rec.authors] == ["Example A Author", "Example B Author"]
    assert rec.issn == ["0000-0019"] and rec.venue == "Example Journal of Mathematics"
    assert http.calls[0].params == {"search_string": "an:9000001"}
    assert ZbmathSource(None).metadata(Ident(IdentKind.DOI, "10.0000/x"), http) is None


def test_oa_and_test() -> None:
    assert ZbmathSource(None).oa_pdf_candidates(Record(title="t", zbmath_id="1"), FakeHttp()) == []
    identify = HttpResponse(
        url=OAI,
        status=200,
        headers={},
        body=b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><Identify/></OAI-PMH>',
    )
    assert ZbmathSource(None).test(FakeHttp().add(OAI, identify)).ok
    assert not ZbmathSource(None).test(FakeHttp().add(OAI, Fail(503))).ok
