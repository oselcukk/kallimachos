"""Archive naming: surnames, et al., short title, year, safe file names."""

from __future__ import annotations

import datetime as dt

import pytest

from kallimachos.archive import naming
from kallimachos.archive.naming import (
    SAFE_NAME_RE,
    authors_part,
    build_filename,
    etal_suffix_for,
    short_title,
    surname,
    year_part,
)
from kallimachos.config.schema import ArchiveConfig, Config
from kallimachos.interfaces import Author, Record


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Example A. Author", "Author"),
        ("Example Author Jr.", "Author"),
        ("Example Author Sr", "Author"),
        ("Example Author III", "Author"),
        ("Example van der Berg", "vanderBerg"),
        ("Example de la Fuente", "delaFuente"),
        ("Example von Beispiel", "vonBeispiel"),
        ("Example SMITHSON", "Smithson"),
        ("Example McExample", "McExample"),
        ("Example Smith-Jones", "SmithJones"),
        ("Example O'Neill", "ONeill"),
        ("Example Müller", "Muller"),
        ("Example Ørsted", "Orsted"),
        ("Example Großmann", "Grossmann"),
        ("Example Łukasiewiczowa", "Lukasiewiczowa"),
        ("Deniz Çağlıoğlu", "Caglioglu"),
        ("Kaan Öztürkçü", "Ozturkcu"),
        ("Example Saint Exemple", "SaintExemple"),
        ("Example Ana Lopez y Ruiz", "LopezyRuiz"),
        ("Exampleson, Example", "Exampleson"),
        ("García Pérez, Example", "GarciaPerez"),
        ("van der Berg, Example", "vanderBerg"),
        ("Single", "Single"),
    ],
)
def test_surname_normalisation(name: str, expected: str) -> None:
    assert surname(name) == expected


def test_surname_compound_list() -> None:
    assert surname("Example Quill Brook") == "Brook"
    assert surname("Example Quill Brook", compound=frozenset({"Quill Brook"})) == "QuillBrook"


def test_packaged_compound_data_is_generic() -> None:
    data = naming.surname_data()
    assert "van" in data.particles and "jr" in data.suffixes
    assert "y" in data.connectors


def test_authors_part_and_etal() -> None:
    cfg = ArchiveConfig()
    assert authors_part(["Example A", "Example B", "Example C"], cfg) == "A-B-C"
    assert authors_part(["Ex One", "Ex Two", "Ex Three", "Ex Four"], cfg) == "One-etal"
    assert authors_part(["Ex One", "Ex Two", "Ex Three", "Ex Four"], cfg, etal_suffix="vd") == (
        "One-vd"
    )
    assert authors_part([], cfg) == "Anonymous"
    cfg2 = ArchiveConfig(max_authors_in_name=1)
    assert authors_part(["Ex One", "Ex Two"], cfg2) == "One-etal"


def test_etal_suffix_vd_only_for_turkish() -> None:
    cfg = Config()
    assert etal_suffix_for(cfg) == "etal"
    cfg.notes.etal_suffix = "vd"
    assert etal_suffix_for(cfg) == "etal"  # English user: vd is not honoured
    cfg.language = "tr"
    assert etal_suffix_for(cfg) == "vd"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("The Structure of Example Loops", "Structure-of-Example-Loops"),
        ("A note on $\\mathbb{Z}_p$-extensions", "Note-on-Zp-Extensions"),
        ("On \\emph{twisted} sums and $L_2$", "On-Twisted-Sums-and-L2"),
        ("An example", "Example"),
        (
            "One two three four five six seven eight nine",
            "One-Two-Three-Four-Five-Six-Seven",
        ),
        ("Cohomology of", "Cohomology"),
        ("", "Untitled"),
        ("Étude des structures", "Etude-Des-Structures"),
    ],
)
def test_short_title(title: str, expected: str) -> None:
    assert short_title(title, max_words=7, max_chars=60) == expected


def test_short_title_char_cap() -> None:
    title = "Extraordinarilylongwordnumberone Extraordinarilylongwordnumbertwo Third"
    out = short_title(title, max_words=7, max_chars=60)
    assert len(out) <= 60
    assert out == "Extraordinarilylongwordnumberone"


def test_year_part() -> None:
    journal = Record(
        title="t",
        doi="10.0000/example.1",
        published=dt.date(2026, 3, 1),
        created=dt.date(2024, 5, 1),
    )
    assert year_part(journal) == "2026"
    preprint = Record(title="t", arxiv_id="2405.00001", created=dt.date(2024, 5, 1))
    assert year_part(preprint) == "2024"
    arxiv_doi = Record(
        title="t",
        doi="10.48550/arXiv.2405.00001",
        published=dt.date(2025, 1, 1),
        created=dt.date(2024, 5, 1),
    )
    assert year_part(arxiv_doi) == "2024"  # arXiv DOI is not a journal year
    assert year_part(Record(title="t")) == "undated"


def test_build_filename_full() -> None:
    rec = Record(
        title="Fictional Results on Example Structures",
        authors=[Author("Example A. Author"), Author("Example B. Writer")],
        arxiv_id="2501.01234",
        created=dt.date(2025, 1, 15),
    )
    name = build_filename(rec, ArchiveConfig(), arxiv_version="v2")
    assert (
        name == "Author-Writer_2025_Fictional-Results-on-Example-Structures_arXiv-2501.01234v2.pdf"
    )
    assert SAFE_NAME_RE.match(name)


def test_build_filename_without_arxiv_and_undated() -> None:
    rec = Record(title="$\\alpha$-Things", authors=[Author("Example One")])
    name = build_filename(rec, ArchiveConfig(), arxiv_version=None)
    assert name == "One_undated_alpha-Things.pdf"


def test_build_filename_old_arxiv_and_many_authors() -> None:
    rec = Record(
        title="Old style",
        authors=[Author(f"Example Person{i}") for i in range(5)],
        arxiv_id="math.GT/0501234v3",
        created=dt.date(2005, 1, 3),
    )
    name = build_filename(rec, ArchiveConfig(), arxiv_version=None, etal_suffix="etal")
    assert name == "Person0-etal_2005_Old-Style_arXiv-math.GT-0501234v3.pdf"
    assert SAFE_NAME_RE.match(name)


def test_build_filename_is_always_safe() -> None:
    rec = Record(
        title="../../etc/passwd; rm -rf / $(whoami)",
        authors=[Author("../Evil ‮Name")],
        created=dt.date(2020, 1, 1),
    )
    name = build_filename(rec, ArchiveConfig(), arxiv_version=None)
    assert SAFE_NAME_RE.match(name)
    assert "/" not in name and ".." not in name.removesuffix(".pdf")


def test_build_filename_unknown_placeholder() -> None:
    cfg = ArchiveConfig(naming_template="{authors}_{journal}")
    with pytest.raises(ValueError):
        build_filename(Record(title="x"), cfg, arxiv_version=None)


def test_person_folder_name() -> None:
    assert naming.person_folder_name("example PERSON Çelik") == "Example-Person-Celik"
