"""process.cite: DOI priority, arXiv version, transliteration-tolerant surname, close runner-ups,
self-citation, same author-year works kept apart."""

from __future__ import annotations

from kallimachos.interfaces import CardCitation, CardReference
from kallimachos.process import cite
from tests.unit.conftest_pdf import (  # noqa: F401
    EID_A,
    EID_B,
    EID_C,
    FakeCatalog,
    fake_store,
    make_card,
    make_entry,
)

T1 = "Spectral gaps of fictional lattice graphs in higher rank"
T2 = "Counting fictional lattice points near smooth curves"
T3 = "Short title"


def _targets() -> dict[str, cite.Target]:
    entries = [
        make_entry(EID_A, title="Source work", authors=["Example Author A"], year=2020),
        make_entry(
            EID_B,
            title=T1,
            authors=["Tschebyscheff, Pafnuty", "Example Author C"],
            year=2018,
            doi="10.0000/example.b",
            arxiv="1801.00001v2",
        ),
        make_entry(EID_C, title=T2, authors=["Chebyshev, Pafnuty"], year=2018, arxiv="1801.00002"),
        make_entry("333333333333", title=T3, authors=["Example Author D"], year=2001),
    ]
    return cite.build_targets(entries, [])


def _card(*refs: str) -> object:
    return make_card(
        EID_A,
        citation=CardCitation(authors=["Example Author A"], year=2020, title="Source work"),
        references=[CardReference(key=f"[{i}]", text=t) for i, t in enumerate(refs, start=1)],
    )


def _match(*refs: str) -> tuple[dict[str, str], dict[str, str]]:
    certain, uncertain = cite.match_against(_card(*refs), _targets())  # type: ignore[arg-type]
    return {e.key: f"{e.target_eid}:{e.method}" for e in certain}, {
        e.key: f"{e.target_eid}:{e.method}" for e in uncertain
    }


def test_surname_key_transliteration() -> None:
    assert cite.surname_key("Tschebyscheff") == cite.surname_key("Chebyshev")
    assert cite.surname_key("Chebyshev, Pafnuty") == cite.surname_key("Pafnuty Chebyshev")
    assert cite.surname_key("Müller") == cite.surname_key("Mueller") == "muler"
    assert cite.surname_key("Wyatt") == cite.surname_key("Viat")


def test_doi_has_priority_over_everything() -> None:
    # the text names the other work's title, but the DOI decides
    certain, uncertain = _match(f"P. Chebyshev, {T2}, 2018. doi:10.0000/example.B")
    assert certain == {"[1]": f"{EID_B}:doi"}
    assert uncertain == {}


def test_arxiv_id_version_stripped() -> None:
    certain, _ = _match("Some text, arXiv:1801.00001v7, 2018.")
    assert certain == {"[1]": f"{EID_B}:arxiv"}
    certain, _ = _match("https://arxiv.org/abs/1801.00002v1")
    assert certain == {"[1]": f"{EID_C}:arxiv"}


def test_title_match_with_transliterated_surname_and_year() -> None:
    certain, uncertain = _match(
        f"P. L. Chebyshev and Example Author C. {T1}. J. Fict. Math. 12 (2019), 1-20."
    )
    assert certain == {"[1]": f"{EID_B}:title"}
    assert uncertain == {}
    # year too far away -> no title match, nothing at all
    certain, uncertain = _match(f"P. L. Chebyshev. {T1}. 2005.")
    assert certain == {} and uncertain == {}


def test_same_author_year_works_are_kept_apart() -> None:
    certain, uncertain = _match(f"Chebyshev, {T1}, 2018.", f"Chebyshev, {T2}, 2018.")
    assert certain == {"[1]": f"{EID_B}:title", "[2]": f"{EID_C}:title"}
    assert uncertain == {}


def test_author_year_alone_is_uncertain_and_close_runner_up() -> None:
    # same surname + year, title words partially shared by both works -> uncertain only
    certain, uncertain = _match("Chebyshev, Spectral gaps of fictional lattice graphs, 2018.")
    assert certain == {}
    assert set(uncertain) == {"[1]"}
    assert uncertain["[1]"].endswith(":author_year_title")


def test_short_title_is_uncertain_and_self_citation_dropped() -> None:
    certain, uncertain = _match(
        f"Example Author D, {T3}, 2001.", "Example Author A, Source work, 2020."
    )
    assert certain == {}
    assert set(uncertain) == {"[1]"}  # [2] is the card's own work -> dropped


def test_build_targets_merges_cards() -> None:
    cards = [
        make_card(
            "444444444444",
            citation=CardCitation(
                authors=["Example Author E"],
                year=1999,
                title="Only in a card",
                arxiv="arXiv:math/9901001v3",
            ),
        )
    ]
    targets = cite.build_targets([make_entry(EID_A, title="", authors=[], year=None)], cards)
    assert targets["444444444444"].arxiv == "math/9901001"
    assert targets["444444444444"].year == 1999
    assert targets[EID_A].title == ""  # catalog entry kept, nothing invented
