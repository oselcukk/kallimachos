from __future__ import annotations

import datetime as dt
import json

from kallimachos.config.schema import CrossrefConfig
from kallimachos.interfaces import ConfigError, HttpResponse, Ident, IdentKind, Record, WorkKind
from kallimachos.sources.crossref import CrossrefSource, full_journal_issns
from tests.unit.test_sources_fakes import Fail, FakeHttp, example_profile, fixture, window

JOURNAL = "https://api.crossref.org/journals/0000-0001/works"
WORK = "https://api.crossref.org/works/10.0000/example.11"


def _page(items: int, cursor: str | None) -> HttpResponse:
    base = json.loads(fixture("crossref", "journal_works").body)
    base["message"]["items"] = (base["message"]["items"] * 10)[:items]
    for i, it in enumerate(base["message"]["items"]):
        it["DOI"] = f"10.0000/example.page.{cursor}.{i}"
    base["message"]["next-cursor"] = cursor
    return HttpResponse(url=JOURNAL, status=200, headers={}, body=json.dumps(base).encode())


def test_discover_parses_and_filters() -> None:
    http = FakeHttp().add(JOURNAL, fixture("crossref", "journal_works"))
    src = CrossrefSource(CrossrefConfig(rows=200))
    w = window("2026-10-17T00:00", "2026-10-18T00:00")
    recs = list(src.discover(w, example_profile(), http))
    assert len(recs) == 3
    first = recs[0]
    assert first.title == "An invented family of Diophantine equations"
    assert first.doi == "10.0000/example.11"
    assert first.abstract and first.abstract.startswith("We prove fictional bounds")
    assert first.abstract_source == "crossref"
    assert first.authors[0].name == "Example Author A"
    assert first.venue == "Example Journal of Mathematics"
    assert first.published == dt.date(2026, 10, 2)
    assert first.kind is WorkKind.ARTICLE
    assert "0000-0001" in first.issn  # the queried ISSN is always attached
    assert recs[2].published == dt.date(2026, 9, 1)
    params = http.calls[0].params
    assert params["cursor"] == "*"
    assert params["rows"] == "200"
    assert "from-index-date:2026-10-17" in params["filter"]
    assert "from-created-date:2026-09-18" in params["filter"]
    assert "type:journal-article" in params["filter"]
    assert "mailto" not in params


def test_only_full_journals_are_queried() -> None:
    assert full_journal_issns(example_profile()) == ["0000-0001"]


def test_cursor_paging_stop_rule() -> None:
    http = FakeHttp(repeat_last=False).add(JOURNAL, _page(3, "c1"), _page(3, "c2"), _page(2, "c3"))
    src = CrossrefSource(CrossrefConfig(rows=3))
    items = list(src._pages(http, JOURNAL, "f", 3))
    assert len(items) == 8
    assert [c.params["cursor"] for c in http.calls] == ["*", "c1", "c2"]


def test_paging_stops_without_cursor() -> None:
    http = FakeHttp().add(JOURNAL, _page(3, None))
    assert len(list(CrossrefSource(None)._pages(http, JOURNAL, "f", 3))) == 3
    assert len(http.calls) == 1


def test_mailto_only_when_configured() -> None:
    http = FakeHttp().add(JOURNAL, fixture("crossref", "journal_works"))
    src = CrossrefSource(
        CrossrefConfig(mailto_ref="env:EXAMPLE_MAILTO"),
        resolve_secret=lambda ref: "reader@example.org",
    )
    list(src.discover(window("2026-10-17T00:00", "2026-10-18T00:00"), example_profile(), http))
    assert http.calls[0].params["mailto"] == "reader@example.org"
    assert "reader@example.org" not in http.calls[0].url
    assert "User-Agent" not in http.calls[0].headers


def test_missing_mailto_secret_is_a_warning() -> None:
    def missing(ref: str) -> str:
        raise ConfigError("missing")

    http = FakeHttp().add(JOURNAL, fixture("crossref", "journal_works"))
    src = CrossrefSource(CrossrefConfig(mailto_ref="env:NOPE"), resolve_secret=missing)
    recs = list(
        src.discover(window("2026-10-17T00:00", "2026-10-18T00:00"), example_profile(), http)
    )
    assert len(recs) == 3 and src.warnings and "mailto" not in http.calls[0].params


def test_metadata_and_oa_candidates() -> None:
    http = FakeHttp().add(WORK, fixture("crossref", "work"))
    src = CrossrefSource(CrossrefConfig())
    rec = src.metadata(Ident(IdentKind.DOI, "10.0000/example.11"), http)
    assert rec is not None and rec.title == "An invented family of Diophantine equations"
    assert http.calls[0].kind == "single"
    assert src.metadata(Ident(IdentKind.ARXIV, "2610.00001"), http) is None
    # no open licence in the work record -> no candidate
    assert src.oa_pdf_candidates(Record(title="t", doi="10.0000/example.11"), http) == []
    # licensed PDF link from the list fixture
    item = json.loads(fixture("crossref", "journal_works").body)["message"]["items"][1]
    body = json.dumps({"status": "ok", "message": item}).encode()
    http2 = FakeHttp().add(
        "https://api.crossref.org/works/10.0000/example.12",
        HttpResponse(url="x", status=200, headers={}, body=body),
    )
    cands = src.oa_pdf_candidates(Record(title="t", doi="10.0000/example.12"), http2)
    assert [(c.url, c.license) for c in cands] == [
        ("https://journal.example.org/pdf/12.pdf", "https://creativecommons.org/licenses/by/4.0/")
    ]


def test_test() -> None:
    ok = CrossrefSource(None).test(
        FakeHttp().add("https://api.crossref.org/works", fixture("crossref", "work"))
    )
    assert ok.ok
    bad = CrossrefSource(None).test(FakeHttp().add("https://api.crossref.org/works", Fail(429)))
    assert not bad.ok and bad.http_status == 429
