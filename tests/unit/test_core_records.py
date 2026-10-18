"""records: identifiers, pool keys, candidate ids, pool merging, paratext."""

from __future__ import annotations

import datetime as dt

import pytest

from kallimachos.interfaces import Author, Ident, IdentKind, Record, WorkKind
from kallimachos.records.keys import candidate_id, keys_for, strip_arxiv_version
from kallimachos.records.model import primary_ident, strong_idents
from kallimachos.records.paratext import classify
from kallimachos.records.pool import Pool, merge

LONG_TITLE = "On a Fictional Bound for Example Character Sums"


def rec(**kw: object) -> Record:
    kw.setdefault("title", LONG_TITLE)
    return Record(**kw)  # type: ignore[arg-type]


# --- identifiers -------------------------------------------------------------------------------


def test_strong_idents_order_and_normalisation() -> None:
    r = rec(
        doi="https://doi.org/10.0000/EXAMPLE.1",
        arxiv_id="arXiv:2510.01234v3",
        eprint_id="2026/123",
        openalex_id="https://openalex.org/W123",
        zbmath_id="1234.11001",
        pubmed_id="99",
        s2_id="abc",
    )
    assert [i.key for i in strong_idents(r)] == [
        "doi:10.0000/example.1",
        "arxiv:2510.01234",
        "eprint:2026/123",
        "oa:W123",
        "zb:1234.11001",
        "pmid:99",
        "s2:abc",
    ]
    assert primary_ident(r) == Ident(IdentKind.DOI, "10.0000/example.1")


def test_arxiv_doi_is_not_a_doi_key_but_an_arxiv_key() -> None:
    r = rec(doi="10.48550/arXiv.2510.01234")
    assert keys_for(r)[0] == "arxiv:2510.01234"
    assert not any(k.startswith("doi:10.48550") for k in keys_for(r))
    assert candidate_id(r) == "arXiv:2510.01234"


def test_title_key_only_when_longer_than_20_chars() -> None:
    assert keys_for(rec(title="Short title here")) == []  # 16 chars
    assert keys_for(rec(title="Exactly twenty chars")) == []  # == 20, not > 20
    long_keys = keys_for(rec(title="Über eine Fiktive Schranke!"))
    assert long_keys == ["nt:uber eine fiktive schranke"]
    assert primary_ident(rec(title="tiny")) is None


def test_keys_include_title_after_strong_ids() -> None:
    keys = keys_for(rec(arxiv_id="2510.01234v2"))
    assert keys == ["arxiv:2510.01234", "nt:on a fictional bound for example character sums"]


def test_candidate_ids() -> None:
    assert candidate_id(rec(openalex_id="W42")) == "W42"
    assert candidate_id(rec(eprint_id="2026/7")) == "ePrint:2026/7"
    assert candidate_id(rec(zbmath_id="1.2")) == "zbMATH:1.2"
    rss = candidate_id(rec(url="https://feeds.example.org/item/1"))
    assert rss.startswith("rss:") and len(rss) == 12
    assert candidate_id(rec()).startswith("nt:")
    assert Ident.parse_candidate_id(candidate_id(rec(doi="10.0000/x"))).key == "doi:10.0000/x"


def test_strip_arxiv_version() -> None:
    assert strip_arxiv_version("2510.01234v2") == "2510.01234"
    assert strip_arxiv_version("math/0601001v1") == "math/0601001"
    assert strip_arxiv_version("2510.01234") == "2510.01234"


# --- pool --------------------------------------------------------------------------------------


def preprint() -> Record:
    return rec(
        arxiv_id="2510.01234v1",
        doi="10.48550/arXiv.2510.01234",
        abstract="Short abstract.",
        abstract_source="arxiv",
        url="https://arxiv.org/abs/2510.01234",
        published=dt.date(2025, 10, 1),
        kind=WorkKind.PREPRINT,
        categories=["math.NT"],
        authors=[Author("Example Author A"), Author("Example Author B")],
        sources={"arxiv"},
        queries={"cat:math.NT"},
    )


def journal() -> Record:
    return rec(
        doi="10.0000/example.journal.1",
        venue="Journal of Example Studies",
        issn=["0000-0000"],
        url="https://doi.org/10.0000/example.journal.1",
        published=dt.date(2026, 9, 1),
        kind=WorkKind.ARTICLE,
        abstract="A much longer abstract that the journal record provides in full detail.",
        abstract_source="crossref",
        authors=[Author("Example Author A", orcid="0000-0000-0000-0001")],
        sources={"crossref"},
        queries={"issn:0000-0000"},
    )


def test_merge_journal_supplies_publication_fields_longest_abstract_wins() -> None:
    merged = merge(preprint(), journal())
    assert merged.doi == "10.0000/example.journal.1"
    assert merged.venue == "Journal of Example Studies"
    assert merged.url == "https://doi.org/10.0000/example.journal.1"
    assert merged.published == dt.date(2026, 9, 1)
    assert merged.issn == ["0000-0000"]
    assert merged.arxiv_id == "2510.01234v1"
    assert merged.abstract_source == "crossref"
    assert merged.kind is WorkKind.ARTICLE
    assert merged.sources == {"arxiv", "crossref"}
    assert merged.queries == {"cat:math.NT", "issn:0000-0000"}
    assert len(merged.authors) == 2 and merged.authors[0].orcid == "0000-0000-0000-0001"
    # order does not matter for the journal rule
    other = merge(journal(), preprint())
    assert other.doi == merged.doi and other.venue == merged.venue and other.url == merged.url


def test_merge_longest_abstract_from_preprint() -> None:
    p = preprint()
    p.abstract = "x" * 500
    j = journal()
    merged = merge(j, p)
    assert merged.abstract == "x" * 500 and merged.abstract_source == "arxiv"
    assert merged.doi == j.doi  # publication fields still from the journal


def test_merge_does_not_mutate_inputs() -> None:
    p, j = preprint(), journal()
    merge(p, j)
    assert p.doi == "10.48550/arXiv.2510.01234" and p.sources == {"arxiv"}


def test_pool_merges_by_title_and_ids() -> None:
    pool = Pool()
    pool.add(preprint())
    pooled = pool.add(journal())  # different strong ids, same long title
    assert len(pool) == 1 and pooled.doi == "10.0000/example.journal.1"
    # a third record with only the journal DOI joins the same work
    third = rec(
        title="Different Title Wording Here", doi="10.0000/EXAMPLE.JOURNAL.1", sources={"openalex"}
    )
    assert pool.add(third).sources == {"arxiv", "crossref", "openalex"}
    assert len(pool) == 1
    assert pool.get("arxiv:2510.01234") is pool.records()[0]


def test_pool_bridging_record_merges_two_entries() -> None:
    pool = Pool()
    pool.add(rec(title="First Fictional Record Title", doi="10.0000/a", sources={"crossref"}))
    pool.add(rec(title="Second Fictional Record Name", arxiv_id="2510.09999", sources={"arxiv"}))
    assert len(pool) == 2
    bridge = rec(title="tiny", doi="10.0000/a", arxiv_id="2510.09999", sources={"openalex"})
    merged = pool.add(bridge)
    assert len(pool) == 1
    assert merged.sources == {"crossref", "arxiv", "openalex"}
    assert pool.get("doi:10.0000/a") is pool.get("arxiv:2510.09999") is merged


def test_pool_keeps_distinct_works() -> None:
    pool = Pool()
    pool.add(rec(title="A Fictional Record About Primes", doi="10.0000/1"))
    pool.add(rec(title="A Fictional Record About Graphs", doi="10.0000/2"))
    pool.add(rec(title="tiny"))  # no key at all: always a new entry
    assert len(pool) == 3


# --- paratext ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Editorial Board",
        "ERRATUM: On a fictional bound",
        "Corrigendum to a fictional paper",
        "Preface",
        "Obituary: Example Author C",
        "Front Matter",
    ],
)
def test_paratext_titles(title: str) -> None:
    assert classify(rec(title=title)) == "paratext"


def test_title_equal_to_venue_is_paratext() -> None:
    assert (
        classify(rec(title="Journal of Example Studies", venue="Journal of Example Studies"))
        == "paratext"
    )


def test_repository_venues_and_dois() -> None:
    assert (
        classify(rec(venue="Zenodo (CERN European Organization for Nuclear Research)"))
        == "repository"
    )
    assert classify(rec(venue="SSRN Electronic Journal")) == "repository"
    assert classify(rec(doi="10.5281/zenodo.123")) == "repository"
    assert (
        classify(
            rec(venue="Example Preprint Server"), extra_repositories=["example preprint server"]
        )
        == "repository"
    )


def test_regular_record_and_extra_patterns() -> None:
    plain = rec(venue="Journal of Example Studies")
    assert classify(plain) is None
    assert (
        classify(
            rec(title="Book review: A fictional monograph"), extra_paratext=[r"\bbook review\b"]
        )
        == "paratext"
    )
    assert (
        classify(rec(title="Retraction note (fictional)"), extra_paratext=["retraction note ("])
        == "paratext"
    )
