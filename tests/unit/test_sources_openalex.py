from __future__ import annotations

import datetime as dt

from kallimachos.config.schema import OpenAlexConfig
from kallimachos.interfaces import Ident, IdentKind, Record, WorkKind
from kallimachos.profile.schema import Profile
from kallimachos.sources.base import run_source
from kallimachos.sources.openalex import OpenAlexSource, abstract_from_index, boolean_operators
from tests.unit.test_sources_fakes import Fail, FakeHttp, example_profile, fixture, window

WORKS = "https://api.openalex.org/works"
KEY = "fake-openalex-key-0001"
W = window("2026-10-10T00:00", "2026-10-11T00:00")


def _profile() -> Profile:
    prof = example_profile()
    prof.channels[0].journals[0].openalex_source_id = "https://openalex.org/S4000000001"
    prof.channels[0].journals[1].openalex_source_id = "S4000000002"
    prof.authors.follow[0].openalex_id = "A5000000010"
    prof.seeds[0].openalex_id = "W2000000001"
    return prof


class Gate:
    def __init__(self, allowed: int) -> None:
        self.allowed = allowed
        self.asked: list[tuple[str, bool]] = []

    def can_spend(self, kind: str, *, weekly: bool = False) -> bool:
        self.asked.append((kind, weekly))
        if self.allowed <= 0:
            return False
        self.allowed -= 1
        return True


def test_discover_queries_and_parse() -> None:
    http = FakeHttp().add(WORKS, fixture("openalex", "works_list"))
    src = OpenAlexSource(OpenAlexConfig())
    recs = list(src.discover(W, _profile(), http))
    assert [r.openalex_id for r in recs] == ["W1000000001", "W1000000002", "W1000000003"]
    first = recs[0]
    assert first.title == "Fictional bounds for twisted exponential sums"
    assert first.doi == "10.0000/example.21"
    assert first.arxiv_id == "2610.00001"
    assert first.authors[0].orcid == "0000-0002-1825-0097"
    assert first.authors[0].openalex_id == "A5000000010"
    assert first.abstract and first.abstract.startswith("We prove fictional bounds")
    assert first.published == dt.date(2026, 10, 12)
    assert first.referenced_works == ["W2000000001", "W2000000002"]
    assert first.kind is WorkKind.ARTICLE
    assert recs[2].abstract is None
    filters = [c.params["filter"] for c in http.calls]
    assert any(f.startswith("primary_location.source.id:S4000000001,") for f in filters)
    assert any(f.startswith("primary_location.source.id:S4000000002,") for f in filters)
    assert any(f.startswith("authorships.author.id:A5000000010,") for f in filters)
    assert any(f.startswith("cites:W2000000001,") for f in filters)
    assert any(
        f.startswith('title_and_abstract.search:"riemann zeta function" OR') for f in filters
    )
    assert all("from_publication_date:2026-10-10" in f for f in filters)
    kinds = {c.params["filter"].split(":")[0]: c.kind for c in http.calls}
    assert kinds["title_and_abstract.search"] == "search"
    assert kinds["cites"] == "list"
    assert all(c.params["per-page"] == "200" and c.params["cursor"] == "*" for c in http.calls)
    # every query matched the same three works: tags are merged, records not duplicated
    assert {"openalex:journal_full", "openalex:seed_citation"} <= recs[0].queries


def test_premium_filter_switch() -> None:
    src = OpenAlexSource(OpenAlexConfig(premium_filters=True))
    assert src.date_filter(W) == "from_created_date:2026-10-10"
    assert OpenAlexSource(None).date_filter(W) == "from_publication_date:2026-10-10"


def test_key_only_in_authorization_header() -> None:
    http = FakeHttp().add(WORKS, fixture("openalex", "works_list"))
    src = OpenAlexSource(OpenAlexConfig(api_key_ref="env:OA_KEY"), resolve_secret=lambda r: KEY)
    list(src.discover(W, _profile(), http))
    for call in http.calls:
        assert call.headers == {"Authorization": f"Bearer {KEY}"}
        assert KEY not in call.url and KEY not in str(call.params)
        assert "api_key" not in call.params


def test_cost_usd_is_summed_and_reported() -> None:
    http = FakeHttp().add(WORKS, fixture("openalex", "works_list"))
    run = run_source(OpenAlexSource(OpenAlexConfig()), W, _profile(), http)
    calls = len(http.calls)
    assert run.stats.budget == round(0.0001 * calls, 6)
    assert run.stats.requests == calls and run.stats.count == 3


def test_budget_gate_stops_paid_calls() -> None:
    http = FakeHttp().add(WORKS, fixture("openalex", "works_list"))
    gate = Gate(allowed=2)
    src = OpenAlexSource(OpenAlexConfig(), budget=gate, weekly=True)
    run = run_source(src, W, _profile(), http)
    assert len(http.calls) == 2
    assert any("budget exhausted" in e for e in run.stats.errors)
    assert gate.asked[0][1] is True  # weekly flag is passed through
    # single-entity calls are free and never ask the gate
    single = FakeHttp().add(WORKS, fixture("openalex", "work_single"))
    src2 = OpenAlexSource(OpenAlexConfig(), budget=Gate(allowed=0))
    assert src2.metadata(Ident(IdentKind.OPENALEX, "W1000000001"), single) is not None


def test_429_stops_the_source() -> None:
    http = FakeHttp().add(WORKS, Fail(429))
    run = run_source(OpenAlexSource(OpenAlexConfig()), W, _profile(), http)
    assert len(http.calls) == 1
    assert any("429" in e for e in run.stats.errors)


def test_boolean_heavy_search_is_spaced() -> None:
    sleeps: list[float] = []
    now = [100.0]
    src = OpenAlexSource(OpenAlexConfig(), sleep=sleeps.append, clock=lambda: now[0])
    http = FakeHttp().add(WORKS, fixture("openalex", "works_list"))
    list(src.discover(W, _profile(), http))
    searches = [c for c in http.calls if c.kind == "search"]
    assert len(searches) == 3  # one per channel; A and B have >5 OR operators, C has 5
    assert sleeps == [1.0]  # the second heavy search waits; the first and C do not
    assert boolean_operators('"a" OR "b" AND NOT "c"') == 3
    assert boolean_operators("or and not") == 0


def test_abstract_from_inverted_index() -> None:
    assert abstract_from_index({"b": [1], "a": [0], "c": [2, 3]}) == "a b c c"
    assert abstract_from_index(None) is None


def test_metadata_routes() -> None:
    http = FakeHttp().add(WORKS, fixture("openalex", "work_single"))
    src = OpenAlexSource(OpenAlexConfig())
    rec = src.metadata(Ident(IdentKind.OPENALEX, "W1000000001"), http)
    assert rec is not None and rec.title == "Fictional bounds for twisted exponential sums"
    src.metadata(Ident(IdentKind.DOI, "10.0000/example.21"), http)
    src.metadata(Ident(IdentKind.ARXIV, "2610.00001"), http)
    src.metadata(Ident(IdentKind.PUBMED, "10000001"), http)
    assert [c.url for c in http.calls] == [
        f"{WORKS}/W1000000001",
        f"{WORKS}/doi:10.0000/example.21",
        f"{WORKS}/doi:10.48550/arxiv.2610.00001",
        f"{WORKS}/pmid:10000001",
    ]
    assert all(c.kind == "single" for c in http.calls)
    assert src.metadata(Ident(IdentKind.ZBMATH, "1"), http) is None


def test_oa_candidates_from_locations() -> None:
    http = FakeHttp().add(WORKS, fixture("openalex", "work_single"))
    cands = OpenAlexSource(None).oa_pdf_candidates(
        Record(title="t", openalex_id="W1000000001"), http
    )
    assert [(c.url, c.source, c.license, c.is_pdf) for c in cands] == [
        ("https://arxiv.org/pdf/2610.00001", "openalex", "cc-by", True)
    ]
    assert OpenAlexSource(None).oa_pdf_candidates(Record(title="t"), FakeHttp()) == []


def test_test_reports_cost_and_key() -> None:
    http = FakeHttp().add(WORKS, fixture("openalex", "works_list"))
    res = OpenAlexSource(OpenAlexConfig()).test(http)
    assert res.ok and res.key_present is False and res.details["cost_usd"] == 0.0001
    assert http.calls[0].params["per-page"] == "1"
