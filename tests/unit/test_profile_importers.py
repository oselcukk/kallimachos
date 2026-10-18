"""Profile: importers (BibTeX, RIS, CSL-JSON, Markdown, PDF) and identifier helpers."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from kallimachos.interfaces import WorkKind
from kallimachos.profile.importers import (
    ImportedRef,
    bibtex,
    clean_doi,
    csl_json,
    find_arxiv,
    find_doi,
    first_author_surname,
    markdown,
    normalize_arxiv,
    pdf_meta,
    ris,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "profile"
LIBRARY = Path(__file__).resolve().parents[1] / "e2e" / "sample_library"


def _load_make_pdf():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("build_fixtures", FIXTURES / "build_fixtures.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.make_pdf


make_pdf = _load_make_pdf()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10.0000/Example.1.", "10.0000/example.1"),
        ("https://doi.org/10.0000/example.2", "10.0000/example.2"),
        ("doi:10.0000/example.3)", "10.0000/example.3"),
        ("10.0000/exa(mple).4)", "10.0000/exa(mple).4"),
    ],
)
def test_clean_doi(raw: str, expected: str) -> None:
    assert clean_doi(raw) == expected


def test_find_doi_and_arxiv_in_text() -> None:
    text = "see doi:10.0000/example.9, and arXiv:2301.00001v2 or arxiv.org/abs/math/0601001"
    assert find_doi(text) == "10.0000/example.9"
    assert find_arxiv(text) == "2301.00001"
    assert normalize_arxiv("2301.00001v3") == "2301.00001"
    assert normalize_arxiv("math/0601001") == "math/0601001"
    assert normalize_arxiv("not an id") is None


def test_first_author_surname_both_orders() -> None:
    assert first_author_surname(["Example Author A"]) == "A"
    assert first_author_surname(["A, Example Author"]) == "A"
    assert first_author_surname([]) == ""


def test_dedupe_key_uses_title_prefix_and_author_prefix() -> None:
    ref = ImportedRef(
        title="Zero Density Estimates for a Family of Dirichlet Series",
        source_file="x",
        authors=["Example Author Bravo"],
    )
    key = ref.dedupe_key
    title_part, author_part = key.split("|")
    assert title_part == "zero density estimates for a family of dirichlet series"[:60]
    assert author_part == "bravo"
    other = ImportedRef(
        title="zero density ESTIMATES for a family of Dirichlet series",
        source_file="y",
        authors=["Bravo, E."],
    )
    assert other.dedupe_key == key


# ---------------------------------------------------------------------------
# BibTeX
# ---------------------------------------------------------------------------


def test_bibtex_sample_library() -> None:
    refs = bibtex.parse(LIBRARY / "references.bib")
    titles = [r.title for r in refs]
    assert "A sieve bound for twin prime pairs in short intervals" in titles
    assert "Zero density estimates for a family of Dirichlet series" in titles  # braces removed
    by_title = {r.title: r for r in refs}
    sieve = by_title["A sieve bound for twin prime pairs in short intervals"]
    assert sieve.doi == "10.0000/example.1"
    assert sieve.authors == ["Example Author A", "Example Author B"]
    assert sieve.year == 2019
    assert sieve.kind is WorkKind.ARTICLE
    assert sieve.venue == "Example Journal of Number Theory"
    assert sieve.tags == ["sieve", "twin primes"]
    large = by_title["Large sieve inequalities for fictional character sums"]
    assert large.arxiv == "2301.00001" and large.kind is WorkKind.PREPRINT
    book = by_title["Introduction to fictional sieve methods"]
    assert book.kind is WorkKind.BOOK and book.venue is None
    zero = by_title["Zero density estimates for a family of Dirichlet series"]
    assert zero.doi == "10.0000/example.5"  # URL form cleaned
    assert len(refs) == 6  # @comment ignored


def test_bibtex_malformed_entries_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "bad.bib"
    path.write_text(
        '@article{ok, title = "Fine {nested {braces}} title", author = {Someone, A.},\n'
        "  year = 2001}\n"
        "@article{notitle, author = {Nobody}, year = {2002}}\n"
        "@article{broken, title = {never closed\n",
        encoding="utf-8",
    )
    refs = bibtex.parse(path)
    assert [r.title for r in refs] == ["Fine nested braces title"]
    assert refs[0].authors == ["Someone, A."]
    assert bibtex.parse(tmp_path / "missing.bib") == []


# ---------------------------------------------------------------------------
# RIS
# ---------------------------------------------------------------------------


def test_ris_sample_library() -> None:
    refs = ris.parse(LIBRARY / "refs.ris")
    assert len(refs) == 3
    first = refs[0]
    assert first.title == "Kloosterman sums and a fictional equidistribution theorem"
    assert first.doi == "10.0000/example.8"
    assert first.authors == ["C, Example Author", "A, Example Author"]
    assert first.year == 2017 and first.kind is WorkKind.ARTICLE
    assert first.venue == "Example Journal of Number Theory"
    assert first.tags == ["Kloosterman sums"]
    assert refs[1].doi is None


def test_ris_continuation_lines_and_types(tmp_path: Path) -> None:
    path = tmp_path / "x.ris"
    path.write_text(
        "TY  - THES\nTI  - A very long fictional thesis title that\n  continues here\n"
        "AU  - Thesis, Writer\nPY  - 2015/01/01\nUR  - https://arxiv.org/abs/1501.00001\nER  -\n"
        "TY  - BOOK\nAU  - Only Author\nER  -\n",
        encoding="utf-8",
    )
    refs = ris.parse(path)
    assert len(refs) == 1
    assert refs[0].title == "A very long fictional thesis title that continues here"
    assert refs[0].kind is WorkKind.THESIS and refs[0].year == 2015
    assert refs[0].arxiv == "1501.00001"


# ---------------------------------------------------------------------------
# CSL-JSON
# ---------------------------------------------------------------------------


def test_csl_json_sample_library_skips_broken_item() -> None:
    refs = csl_json.parse(LIBRARY / "zotero-export.json")
    assert len(refs) == 2
    exp = refs[1]
    assert exp.title == "Exponential sums over fictional finite fields with applications"
    assert exp.doi == "10.0000/example.7" and exp.year == 2018
    assert exp.authors == ["B, Example Author"]
    assert exp.kind is WorkKind.ARTICLE
    assert exp.venue == "Example Mathematical Proceedings"
    assert exp.tags == ["exponential sums", "finite fields"]


def test_csl_json_object_with_items_and_types(tmp_path: Path) -> None:
    path = tmp_path / "items.json"
    path.write_text(
        json.dumps(
            {
                "items": [
                    {"type": "thesis", "title": "Fictional thesis", "issued": {"raw": "2012-05"}},
                    {
                        "type": "manuscript",
                        "title": "Draft",
                        "URL": "https://arxiv.org/abs/2001.00001",
                    },
                    "garbage",
                ]
            }
        ),
        encoding="utf-8",
    )
    refs = csl_json.parse(path)
    assert [(r.kind, r.year) for r in refs] == [(WorkKind.THESIS, 2012), (WorkKind.PREPRINT, None)]
    assert refs[1].arxiv == "2001.00001"
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert csl_json.parse(tmp_path / "bad.json") == []


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def test_markdown_folder_parses_notes_and_skips_index() -> None:
    refs = markdown.parse(LIBRARY / "notes")
    names = {Path(r.source_file).name for r in refs}
    assert "_Index.md" not in names
    by_name = {Path(r.source_file).name: r for r in refs}
    note = by_name["A-2019-Sieve-Bound-Twin-Primes.md"]
    assert note.title == "A sieve bound for twin prime pairs in short intervals"
    assert note.authors == ["Example Author A"] and note.year == 2019
    assert note.doi == "10.0000/example.1"
    assert note.project == "sieves" and "sieve" in note.tags
    assert note.kind is WorkKind.ARTICLE  # from the ``article`` tag
    plan = by_name["Reading-Plan-Fictional-Seminar.md"]
    assert plan.kind is WorkKind.UNKNOWN and plan.doi is None
    person = by_name["Jane-Q-Sample.md"]
    assert person.title == "Jane Q. Sample"


def test_markdown_frontmatter_kind_and_dash_variants(tmp_path: Path) -> None:
    (tmp_path / "n.md").write_text(
        "---\nkind: book\nyear: 1999\n---\n# Writer, Co-Writer (1999) - A fictional book title\n\n"
        "arXiv:1901.00001\n",
        encoding="utf-8",
    )
    ref = markdown.parse(tmp_path / "n.md")[0]
    assert ref.kind is WorkKind.BOOK and ref.year == 1999
    assert ref.authors == ["Writer", "Co-Writer"]
    assert ref.title == "A fictional book title" and ref.arxiv == "1901.00001"
    (tmp_path / "empty.md").write_text("no heading here\n", encoding="utf-8")
    assert markdown.parse(tmp_path / "empty.md") == []


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def test_pdf_builtin_extractor_reads_synthetic_pdfs(tmp_path: Path) -> None:
    make_pdf(
        tmp_path / "a.pdf",
        ["Fictional title with (parentheses) in it", "Author One, Author Two", "doi:10.0000/x.1"],
    )
    text = pdf_meta.builtin_text(tmp_path / "a.pdf")
    assert "Fictional title with (parentheses) in it" in text
    refs = pdf_meta.parse(tmp_path / "a.pdf", tool="builtin")
    assert refs[0].title == "Fictional title with (parentheses) in it"
    assert refs[0].authors == ["Author One", "Author Two"]
    assert refs[0].doi == "10.0000/x.1" and refs[0].kind is WorkKind.ARTICLE


def test_pdf_folder_skip_callback_prevents_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_pdf(tmp_path / "keep.pdf", ["A kept fictional title here", "arXiv:2402.00002"])
    make_pdf(tmp_path / "secret.pdf", ["Secret", "doi:10.0000/secret.1"])
    opened: list[str] = []
    original = Path.read_bytes

    def spy(self: Path) -> bytes:
        opened.append(self.name)
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", spy)
    refs = pdf_meta.parse(tmp_path, tool="builtin", skip=lambda p: "secret" in p.name)
    assert [r.arxiv for r in refs] == ["2402.00002"]
    assert refs[0].kind is WorkKind.PREPRINT
    assert "secret.pdf" not in opened


def test_pdf_sample_library_both_extractors_agree() -> None:
    builtin = pdf_meta.parse(LIBRARY / "pdfs", tool="builtin")
    assert len(builtin) == 2
    assert {r.doi or r.arxiv for r in builtin} == {"10.0000/example.1", "2402.00002"}
    if pdf_meta.pdftotext_available():
        external = pdf_meta.parse(LIBRARY / "pdfs", tool="pdftotext")
        assert [(r.title, r.doi, r.arxiv) for r in external] == [
            (r.title, r.doi, r.arxiv) for r in builtin
        ]


def test_pdf_without_text_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "junk.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    assert pdf_meta.parse(tmp_path / "junk.pdf", tool="builtin") == []
