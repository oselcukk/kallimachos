"""Profile: match confidence table, similarity, lookup order and OpenAlex parsing."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from kallimachos.config.schema import ProfileMatchConfig
from kallimachos.interfaces import Author, HttpError, HttpResponse, Record, WorkKind
from kallimachos.profile.importers import ImportedRef
from kallimachos.profile.match import (
    ARXIV_DOI_PREFIX,
    AutocompleteHit,
    OpenAlexLookup,
    WorkExtra,
    author_matches,
    confidence_for,
    match_reference,
    record_from_openalex,
    title_similarity,
)
from kallimachos.profile.offline import FixtureHttp

CFG = ProfileMatchConfig()
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "profile" / "http"


@pytest.mark.parametrize(
    ("sim", "author", "dy", "expected"),
    [
        (0.95, True, 0, "high"),
        (0.95, False, 0, "medium"),  # sim >= 0.85 and year diff <= 1
        (0.86, True, None, "medium"),
        (0.86, False, 2, None),  # neither author nor close year
        (0.98, False, 3, "medium"),  # strict branch: sim >= 0.97 and |dy| <= 3
        (0.98, False, 4, None),
        (0.80, True, None, "low"),
        (0.80, False, 0, None),
        (0.90, True, 11, "low"),  # medium (author) but year gap > 10 and sim < 0.95 -> low
        (0.96, True, 11, "high"),  # year gap rule only downgrades medium
        (0.90, False, 11, None),  # year diff 11 is not <= 1; sim < 0.97 -> no match
        (0.74, True, 0, None),
    ],
)
def test_confidence_table(sim: float, author: bool, dy: int | None, expected: str | None) -> None:
    assert confidence_for(sim, author, dy, CFG) == expected


def test_confidence_thresholds_come_from_config() -> None:
    cfg = ProfileMatchConfig(high_sim=0.99)
    assert confidence_for(0.95, True, 0, cfg) == "medium"


def test_title_similarity_containment_rule() -> None:
    a = "Zero density estimates for a family of Dirichlet series"
    assert title_similarity(a, a.upper()) == 1.0
    assert title_similarity(a, a + " and some more words at the end here") >= 0.9
    assert title_similarity("short", "short") == 1.0
    assert title_similarity("", "x") == 0.0
    assert title_similarity("completely different words", "zero density estimates") < 0.5


def test_author_matches_accent_and_order_insensitive() -> None:
    record_authors = [Author("Jose Example-Author"), Author("Kerime Ornek")]
    assert author_matches(["Ornek, K."], record_authors)
    assert author_matches(["K. Ornek"], record_authors)
    assert not author_matches(["Somebody Else"], record_authors)
    assert not author_matches([], record_authors)


def _record(title: str, year: int, *authors: str, oa: str = "W1", **kw: object) -> Record:
    return Record(
        title=title,
        authors=[Author(a) for a in authors],
        published=dt.date(year, 1, 1),
        openalex_id=oa,
        **kw,  # type: ignore[arg-type]
    )


class FakeLookup:
    """Scriptable :class:`Lookup`; records the order of calls."""

    def __init__(self) -> None:
        self.by_doi: dict[str, Record] = {}
        self.by_id: dict[str, Record] = {}
        self.ac: list[AutocompleteHit] = []
        self.crossref: str | None = None
        self.s2: str | None = None
        self.calls: list[str] = []
        self.requests: list[str] = []

    def work_by_doi(self, doi: str) -> tuple[Record, WorkExtra] | None:
        self.calls.append(f"doi:{doi}")
        rec = self.by_doi.get(doi)
        return (rec, WorkExtra()) if rec else None

    def work_by_id(self, openalex_id: str) -> tuple[Record, WorkExtra] | None:
        self.calls.append(f"id:{openalex_id}")
        rec = self.by_id.get(openalex_id)
        return (rec, WorkExtra()) if rec else None

    def autocomplete(self, title: str) -> list[AutocompleteHit]:
        self.calls.append("autocomplete")
        return self.ac

    def crossref_doi(self, title: str, author: str | None, year: int | None) -> str | None:
        self.calls.append("crossref")
        return self.crossref

    def s2_doi(self, title: str) -> str | None:
        self.calls.append("s2")
        return self.s2

    def works_by_ids(self, ids: Sequence[str]) -> list[tuple[Record, WorkExtra]]:
        return []

    def works_by_author(self, author_id: str) -> list[tuple[Record, WorkExtra]]:
        return []


HTTP = FixtureHttp()  # never consulted when a lookup is injected
REF = ImportedRef(
    title="Zero density estimates for a family of Dirichlet series",
    source_file="refs.bib",
    year=2020,
    authors=["Example Author B"],
)


def test_doi_path_wins_even_with_moderate_title_similarity() -> None:
    lk = FakeLookup()
    ref = ImportedRef(**{**REF.__dict__, "doi": "10.0000/example.5"})
    lk.by_doi["10.0000/example.5"] = _record(
        "Zero density estimates for Dirichlet series", 2020, "Example Author B", oa="W5"
    )
    m = match_reference(ref, HTTP, CFG, lookup=lk)
    assert m is not None and m.via == "doi" and m.confidence == "high"
    assert lk.calls == ["doi:10.0000/example.5"]


def test_arxiv_id_is_looked_up_as_datacite_doi() -> None:
    lk = FakeLookup()
    ref = ImportedRef(**{**REF.__dict__, "arxiv": "2301.00001"})
    lk.by_doi[f"{ARXIV_DOI_PREFIX}2301.00001"] = _record(REF.title, 2020, "Example Author B")
    m = match_reference(ref, HTTP, CFG, lookup=lk)
    assert m is not None and m.confidence == "high"


def test_wrong_doi_falls_through_to_autocomplete() -> None:
    lk = FakeLookup()
    ref = ImportedRef(**{**REF.__dict__, "doi": "10.0000/wrong"})
    lk.by_doi["10.0000/wrong"] = _record("Completely unrelated fictional paper", 1990, "Nobody")
    lk.ac = [AutocompleteHit("W9", REF.title)]
    lk.by_id["W9"] = _record(REF.title, 2020, "Example Author B", oa="W9")
    m = match_reference(ref, HTTP, CFG, lookup=lk)
    assert m is not None and m.via == "openalex_autocomplete" and m.record.openalex_id == "W9"


def test_autocomplete_filters_by_min_sim_then_crossref() -> None:
    lk = FakeLookup()
    lk.ac = [AutocompleteHit("W2", "Something entirely different about graphs")]
    lk.crossref = "10.0000/example.5"
    lk.by_doi["10.0000/example.5"] = _record(REF.title, 2021, "Example Author B", oa="W5")
    m = match_reference(REF, HTTP, CFG, lookup=lk)
    assert m is not None and m.via == "crossref_bibliographic"
    assert "id:W2" not in lk.calls  # below autocomplete_min_sim: never opened
    assert "s2" not in lk.calls  # no key -> S2 skipped


def test_s2_only_with_key_and_low_match_returned_as_low() -> None:
    lk = FakeLookup()
    lk.ac = [AutocompleteHit("W3", REF.title)]
    lk.by_id["W3"] = _record(
        "Zero density estimates for a family of Dirichlet polynomials",
        1995,
        "Example Author B",
        oa="W3",
    )  # sim ~0.9 but year gap 25 -> low
    lk.s2 = None
    m = match_reference(REF, HTTP, CFG, lookup=lk, s2_enabled=True)
    assert m is not None and m.confidence == "low"
    assert "s2" in lk.calls and "crossref" in lk.calls


def test_no_candidate_returns_none() -> None:
    lk = FakeLookup()
    assert match_reference(REF, HTTP, CFG, lookup=lk) is None


# ---------------------------------------------------------------------------
# OpenAlex parsing and request shapes
# ---------------------------------------------------------------------------


def test_record_from_openalex_fixture() -> None:
    body = json.loads((FIXTURES / "openalex_doi_W1000000001.body.json").read_text("utf-8"))
    record, extra = record_from_openalex(body)
    assert record.openalex_id == "W1000000001"
    assert record.doi == "10.0000/example.1"
    assert record.published == dt.date(2019, 3, 1)
    assert [a.name for a in record.authors] == ["Example Author A", "Example Author B"]
    assert record.authors[0].orcid == "0000-0001-0000-0001"
    assert record.authors[0].openalex_id == "A5000000001"
    assert record.venue == "Example Journal of Number Theory" and record.issn == ["0000-0001"]
    assert record.referenced_works == ["W2000000001", "W2000000002"]
    assert record.kind is WorkKind.ARTICLE
    assert extra.source_id == "S9000000001" and extra.subfields == ["Analytic Number Theory"]
    assert extra.cited_by_count == 12


def test_record_from_openalex_preprint_has_arxiv_id() -> None:
    body = json.loads((FIXTURES / "openalex_work_W1000000003.body.json").read_text("utf-8"))
    record, _ = record_from_openalex(body)
    assert record.arxiv_id == "2301.00001" and record.kind is WorkKind.PREPRINT


class RecordingHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, str] | None, Mapping[str, str] | None]] = []
        self.errors: list[HttpError] = []

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        tries: int = 3,
        timeout: float = 60.0,
        kind: str = "list",
    ) -> HttpResponse | None:
        self.calls.append((url, params, headers))
        return HttpResponse(url=url, status=200, headers={}, body=b'{"results": [], "message": {}}')


def test_openalex_lookup_request_shapes_carry_no_secrets() -> None:
    http = RecordingHttp()
    lk = OpenAlexLookup(http, s2_enabled=True)
    lk.work_by_doi("10.0000/Example.1")
    lk.work_by_id("https://openalex.org/W5")
    lk.autocomplete("A title\twith control\x01chars " + "x" * 300)
    lk.crossref_doi("Some title", "Surname", 2001)
    lk.s2_doi("Some title")
    lk.works_by_ids([f"W{i}" for i in range(60)])
    lk.works_by_author("A1")
    urls = [c[0] for c in http.calls]
    assert urls[0] == "https://api.openalex.org/works/doi:10.0000/example.1"
    assert urls[1] == "https://api.openalex.org/works/W5"
    assert http.calls[2][1] is not None and len(http.calls[2][1]["q"]) <= 200
    assert "\x01" not in http.calls[2][1]["q"]
    assert http.calls[3][1] == {
        "query.bibliographic": "Some title",
        "rows": "5",
        "query.author": "Surname",
    }
    assert urls[4].startswith("https://api.semanticscholar.org/graph/v1/paper/search/match")
    assert (
        len([u for u in urls if u == "https://api.openalex.org/works"]) == 3
    )  # 2 batches + author
    joined = json.dumps([[u, dict(p or {}), dict(h or {})] for u, p, h in http.calls])
    for forbidden in ("api_key", "Authorization", "x-api-key", "mailto"):
        assert forbidden not in joined
    assert len(lk.requests) == len(http.calls)
