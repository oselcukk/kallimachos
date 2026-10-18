"""Generate the L3 test assets: the fictional ``tests/e2e/sample_library`` and the recorded
OpenAlex/Crossref/ORCID exchanges under ``tests/fixtures/profile/http``.

Everything is fictional (``Example Author A``, ``10.0000/example.N``, ``W1000000001``). Re-run
with ``python tests/fixtures/profile/build_fixtures.py``; the output is committed so tests never
depend on this script at run time.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
LIBRARY = REPO / "tests" / "e2e" / "sample_library"
HTTP = HERE / "http"

OPENALEX = "https://api.openalex.org"
CROSSREF = "https://api.crossref.org"
ORCID_DEMO = "0000-0002-1825-0097"  # ORCID's public demo record id; the content below is fictional

# ---------------------------------------------------------------------------
# Minimal PDF writer (uncompressed, one page, Helvetica)
# ---------------------------------------------------------------------------


def make_pdf(path: Path, lines: list[str]) -> None:
    """Write a one-page PDF whose content stream shows ``lines`` (readable by pdftotext/pypdf
    and by ``pdf_meta.builtin_text``)."""

    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    content_lines = ["BT", "/F1 12 Tf", "72 760 Td", "14 TL"]
    for index, line in enumerate(lines):
        if index:
            content_lines.append("T*")
        content_lines.append(f"({esc(line)}) Tj")
    content_lines.append("ET")
    content = "\n".join(content_lines).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    trailer = f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    out += trailer.encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))


# ---------------------------------------------------------------------------
# Fictional works
# ---------------------------------------------------------------------------

JOURNAL_NT = {
    "id": f"{OPENALEX.replace('api.', '')}/S9000000001",
    "display_name": "Example Journal of Number Theory",
    "issn_l": "0000-0001",
    "issn": ["0000-0001"],
}
JOURNAL_MP = {
    "id": "https://openalex.org/S9000000002",
    "display_name": "Example Mathematical Proceedings",
    "issn_l": "0000-0002",
    "issn": ["0000-0002", "0000-0003"],
}
ARXIV_SOURCE = {
    "id": "https://openalex.org/S9000000009",
    "display_name": "arXiv (Example Mirror)",
    "issn_l": None,
    "issn": [],
}

AUTHORS = {
    "A": ("A5000000001", "Example Author A", "0000-0001-0000-0001"),
    "B": ("A5000000002", "Example Author B", None),
    "C": ("A5000000003", "Example Author C", "0000-0001-0000-0003"),
    "E": ("A5000000005", "Example Author E", None),
    "F": ("A5000000006", "Example Author F", None),
}

FOUNDATIONAL = {
    "W2000000001": "Fictional foundations of sieve theory",
    "W2000000002": "A fictional survey of exponential sums",
    "W2000000003": "Fictional notes on Dirichlet series",
}

WORKS: list[dict[str, Any]] = [
    {
        "id": "W1000000001",
        "title": "A sieve bound for twin prime pairs in short intervals",
        "year": 2019,
        "date": "2019-03-01",
        "doi": "10.0000/example.1",
        "authors": ["A", "B"],
        "source": JOURNAL_NT,
        "topics": [("Sieve Methods", "Analytic Number Theory")],
        "refs": ["W2000000001", "W2000000002"],
        "cited": 12,
    },
    {
        "id": "W1000000002",
        "title": "Moments of a fictional zeta function on the critical line",
        "year": 2021,
        "date": "2021-06-15",
        "doi": "10.0000/example.2",
        "authors": ["C"],
        "source": JOURNAL_MP,
        "topics": [("Zeta Functions", "Analytic Number Theory")],
        "refs": ["W2000000002", "W2000000003"],
        "cited": 4,
    },
    {
        "id": "W1000000003",
        "title": "Large sieve inequalities for fictional character sums",
        "year": 2023,
        "date": "2023-01-02",
        "doi": "10.48550/arxiv.2301.00001",
        "authors": ["A"],
        "source": ARXIV_SOURCE,
        "type": "preprint",
        "topics": [("Sieve Methods", "Analytic Number Theory")],
        "refs": ["W2000000001"],
        "cited": 1,
    },
    {
        "id": "W1000000005",
        "title": "Zero density estimates for a family of Dirichlet series",
        "year": 2020,
        "date": "2020-11-20",
        "doi": "10.0000/example.5",
        "authors": ["B", "C"],
        "source": JOURNAL_NT,
        "topics": [("Zeta Functions", "Analytic Number Theory")],
        "refs": ["W2000000002", "W2000000003"],
        "cited": 7,
    },
    {
        "id": "W1000000007",
        "title": "Exponential sums over fictional finite fields with applications",
        "year": 2018,
        "date": "2018-05-05",
        "doi": "10.0000/example.7",
        "authors": ["B"],
        "source": JOURNAL_MP,
        "topics": [("Exponential Sums", "Combinatorics")],
        "refs": ["W2000000002"],
        "cited": 20,
    },
    {
        "id": "W1000000008",
        "title": "Kloosterman sums and a fictional equidistribution theorem",
        "year": 2017,
        "date": "2017-09-09",
        "doi": "10.0000/example.8",
        "authors": ["C", "A"],
        "source": JOURNAL_NT,
        "topics": [("Exponential Sums", "Analytic Number Theory")],
        "refs": ["W2000000002"],
        "cited": 9,
    },
    {
        "id": "W1000000009",
        "title": "Equidistribution of fictional Kloosterman angles in arithmetic progressions",
        "year": 2016,
        "date": "2016-02-02",
        "doi": None,
        "authors": ["F"],
        "source": JOURNAL_NT,
        "topics": [("Exponential Sums", "Analytic Number Theory")],
        "refs": [],
        "cited": 3,
    },
    {
        "id": "W1000000010",
        "title": "Fictional bounds for additive energy of sparse sets",
        "year": 2022,
        "date": "2022-08-08",
        "doi": "10.0000/example.10",
        "authors": ["F", "A"],
        "source": JOURNAL_MP,
        "topics": [("Additive Combinatorics", "Combinatorics")],
        "refs": ["W2000000001"],
        "cited": 2,
    },
    {
        "id": "W1000000011",
        "title": "Mean values of fictional L-functions along vertical lines",
        "year": 2024,
        "date": "2024-02-02",
        "doi": "10.48550/arxiv.2402.00002",
        "authors": ["E"],
        "source": ARXIV_SOURCE,
        "type": "preprint",
        "topics": [("Zeta Functions", "Analytic Number Theory")],
        "refs": ["W2000000003"],
        "cited": 0,
    },
]


def openalex_work(w: dict[str, Any]) -> dict[str, Any]:
    authorships = []
    for key in w["authors"]:
        oa_id, name, orcid = AUTHORS[key]
        authorships.append(
            {
                "author": {
                    "id": f"https://openalex.org/{oa_id}",
                    "display_name": name,
                    "orcid": f"https://orcid.org/{orcid}" if orcid else None,
                }
            }
        )
    return {
        "id": f"https://openalex.org/{w['id']}",
        "doi": f"https://doi.org/{w['doi']}" if w["doi"] else None,
        "display_name": w["title"],
        "title": w["title"],
        "publication_year": w["year"],
        "publication_date": w["date"],
        "type": w.get("type", "article"),
        "type_crossref": "posted-content" if w.get("type") == "preprint" else "journal-article",
        "authorships": authorships,
        "primary_location": {
            "source": w["source"],
            "landing_page_url": f"https://example.org/works/{w['id']}",
        },
        "topics": [{"display_name": t, "subfield": {"display_name": s}} for t, s in w["topics"]],
        "keywords": [{"display_name": t} for t, _ in w["topics"]],
        "referenced_works": [f"https://openalex.org/{r}" for r in w["refs"]],
        "cited_by_count": w["cited"],
    }


def foundational_work(work_id: str, title: str) -> dict[str, Any]:
    return {
        "id": f"https://openalex.org/{work_id}",
        "doi": None,
        "display_name": title,
        "title": title,
        "publication_year": 1999,
        "publication_date": "1999-01-01",
        "type": "article",
        "authorships": [
            {
                "author": {
                    "id": "https://openalex.org/A5000000099",
                    "display_name": "Example Author Z",
                }
            }
        ],
        "primary_location": {"source": JOURNAL_NT},
        "topics": [],
        "referenced_works": [],
        "cited_by_count": 100,
    }


# ---------------------------------------------------------------------------
# Fixture writer (tests/fixtures/README.md format)
# ---------------------------------------------------------------------------


def write_case(
    name: str,
    url: str,
    params: dict[str, str] | None,
    body: Any,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> None:
    HTTP.mkdir(parents=True, exist_ok=True)
    body_name = f"{name}.body.json"
    (HTTP / body_name).write_text(
        json.dumps(body, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    case = {
        "request": {"method": "GET", "url": url, "params": params or {}},
        "response": {
            "status": status,
            "headers": headers or {"Content-Type": "application/json"},
            "body_file": body_name,
        },
    }
    (HTTP / f"{name}.json").write_text(
        json.dumps(case, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )


def build_http_fixtures() -> None:
    if HTTP.exists():
        shutil.rmtree(HTTP)
    for w in WORKS:
        if w["doi"]:
            write_case(
                f"openalex_doi_{w['id']}",
                f"{OPENALEX}/works/doi:{w['doi']}",
                None,
                openalex_work(w),
            )
        write_case(
            f"openalex_work_{w['id']}", f"{OPENALEX}/works/{w['id']}", None, openalex_work(w)
        )
    # autocomplete for the title-only RIS record (W1000000009)
    w9 = next(w for w in WORKS if w["id"] == "W1000000009")
    write_case(
        "openalex_autocomplete_W1000000009",
        f"{OPENALEX}/autocomplete/works",
        {"q": w9["title"]},
        {
            "results": [
                {
                    "id": "https://openalex.org/W1000000009",
                    "display_name": w9["title"],
                    "hint": "Example Author F",
                },
                {
                    "id": "https://openalex.org/W1000000099",
                    "display_name": "An unrelated fictional title about graphs",
                    "hint": "Example Author Z",
                },
            ]
        },
    )
    # Crossref bibliographic search for the second title-only RIS record (W1000000010)
    w10 = next(w for w in WORKS if w["id"] == "W1000000010")
    write_case(
        "crossref_bibliographic_W1000000010",
        f"{CROSSREF}/works",
        {"query.bibliographic": w10["title"], "rows": "5", "query.author": "F"},
        {
            "message": {
                "items": [
                    {
                        "DOI": w10["doi"],
                        "title": [w10["title"]],
                        "author": [{"family": "F", "given": "Example Author"}],
                        "issued": {"date-parts": [[2022]]},
                    }
                ]
            }
        },
    )
    # foundational titles (batched by id, most-cited-by-seeds first)
    write_case(
        "openalex_foundational_batch",
        f"{OPENALEX}/works",
        {"filter": "openalex_id:W2000000002|W2000000001|W2000000003", "per-page": "50"},
        {"results": [foundational_work(i, t) for i, t in FOUNDATIONAL.items()]},
    )
    # yearly volumes for the fixed test year 2025
    write_case(
        "openalex_volume_S9000000001_2025",
        f"{OPENALEX}/works",
        {"filter": "primary_location.source.id:S9000000001,publication_year:2025", "per-page": "1"},
        {"meta": {"count": 120}, "results": []},
    )
    write_case(
        "openalex_volume_S9000000002_2025",
        f"{OPENALEX}/works",
        {"filter": "primary_location.source.id:S9000000002,publication_year:2025", "per-page": "1"},
        {"meta": {"count": 800}, "results": []},
    )
    # ORCID works summary (fictional content under the public demo id)
    groups = []
    for w in [*WORKS[:2], WORKS[3]]:
        groups.append(
            {
                "external-ids": {
                    "external-id": [{"external-id-type": "doi", "external-id-value": w["doi"]}]
                },
                "work-summary": [
                    {
                        "title": {"title": {"value": w["title"]}},
                        "type": "journal-article",
                        "publication-date": {"year": {"value": str(w["year"])}},
                        "journal-title": {"value": w["source"]["display_name"]},
                        "external-ids": {
                            "external-id": [
                                {"external-id-type": "doi", "external-id-value": w["doi"]}
                            ]
                        },
                    }
                ],
            }
        )
    groups.append(
        {
            "external-ids": {"external-id": []},
            "work-summary": [
                {
                    "title": {"title": {"value": "Short note"}},
                    "type": "other",
                    "publication-date": None,
                    "journal-title": None,
                    "external-ids": {"external-id": []},
                }
            ],
        }
    )
    write_case(
        "orcid_works_demo",
        f"https://pub.orcid.org/v3.0/{ORCID_DEMO}/works",
        None,
        {"group": groups},
    )
    # OpenAlex author works (one page)
    write_case(
        "openalex_author_A5000000001_page1",
        f"{OPENALEX}/works",
        {"filter": "author.id:A5000000001", "per-page": "200", "cursor": "*"},
        {
            "meta": {"count": 3, "next_cursor": None},
            "results": [openalex_work(w) for w in WORKS if "A" in w["authors"]][:3],
        },
    )


# ---------------------------------------------------------------------------
# Sample library
# ---------------------------------------------------------------------------

BIB = """% Fictional references for the Kallimachos test-suite. No real people or papers.
@article{exA2019sieve,
  author  = {Example Author A and Example Author B},
  title   = {A sieve bound for twin prime pairs in short intervals},
  journal = {Example Journal of Number Theory},
  year    = {2019},
  volume  = {12},
  pages   = {1--20},
  doi     = {10.0000/example.1},
  keywords = {sieve, twin primes}
}

@article{exC2021moments,
  author  = {Example Author C},
  title   = {Moments of a fictional zeta function on the critical line},
  journal = {Example Mathematical Proceedings},
  year    = 2021,
  doi     = "10.0000/example.2"
}

@misc{exA2023large,
  author        = {Example Author A},
  title         = {Large sieve inequalities for fictional character sums},
  year          = {2023},
  eprint        = {2301.00001},
  archivePrefix = {arXiv},
  primaryClass  = {math.NT}
}

@book{exD2010intro,
  author    = {Example Author D},
  title     = {Introduction to fictional sieve methods},
  publisher = {Example University Press},
  year      = {2010}
}

@article{exBC2020zero,
  author  = {Example Author B and Example Author C},
  title   = {Zero density estimates for a family of {Dirichlet} series},
  journal = {Example Journal of Number Theory},
  year    = {2020},
  doi     = {https://doi.org/10.0000/example.5}
}

@misc{exD2015lecture,
  author = {Example Author D},
  title  = {Lecture notes on sieve theory},
  year   = {2015},
  note   = {Course handout, fictional university}
}

@comment{this block is ignored by the importer}
"""

CSL_JSON = [
    {
        "id": "exC2021moments",
        "type": "article-journal",
        "title": "Moments of a fictional zeta function on the critical line",
        "container-title": "Example Mathematical Proceedings",
        "author": [{"family": "C", "given": "Example Author"}],
        "issued": {"date-parts": [[2021, 6, 15]]},
        "DOI": "10.0000/example.2",
    },
    {
        "id": "exB2018exp",
        "type": "article-journal",
        "title": "Exponential sums over fictional finite fields with applications",
        "container-title": "Example Mathematical Proceedings",
        "author": [{"family": "B", "given": "Example Author"}],
        "issued": {"date-parts": [[2018]]},
        "DOI": "10.0000/example.7",
        "keyword": "exponential sums; finite fields",
    },
    {"id": "broken", "type": "article-journal", "author": "not a list"},
]

RIS = """TY  - JOUR
AU  - C, Example Author
AU  - A, Example Author
TI  - Kloosterman sums and a fictional equidistribution theorem
JO  - Example Journal of Number Theory
PY  - 2017
DO  - 10.0000/example.8
KW  - Kloosterman sums
ER  -

TY  - JOUR
AU  - F, Example Author
TI  - Equidistribution of fictional Kloosterman angles in arithmetic progressions
JO  - Example Journal of Number Theory
PY  - 2016
ER  -

TY  - JOUR
AU  - F, Example Author
AU  - A, Example Author
TI  - Fictional bounds for additive energy of sparse sets
JO  - Example Mathematical Proceedings
PY  - 2022
ER  -
"""

NOTES = {
    "A-2019-Sieve-Bound-Twin-Primes.md": """---
project: sieves
tags: [article, sieve]
---
# Example Author A (2019) — A sieve bound for twin prime pairs in short intervals

## Citation
Example Journal of Number Theory 12 (2019), 1-20. doi:10.0000/example.1

## Summary
Fictional note used by the test-suite.
""",
    "Reading-Plan-Fictional-Seminar.md": """---
project: sieves
tags: [plan]
---
# Reading plan for the fictional seminar

No identifiers here; this note must not be queried.
""",
    "Jane-Q-Sample.md": """# Jane Q. Sample

A person page. Person-name-like titles never leave the machine.
""",
    "_Index.md": """# Index

Index notes starting with an underscore are skipped.
""",
}

PRIVATE_BIB = """@article{privateGrades,
  author = {Example Student},
  title = {A private record that must never be opened by the profile builder},
  year = {2024},
  doi = {10.0000/private.1}
}
"""

PRIVATE_NOTE = """# Example Student (2024) — Grades and personal remarks

doi:10.0000/private.2
"""

README = """# Fictional sample library (profile-building tests)

Every reference, author and identifier here is invented (`Example Author A`,
`10.0000/example.N`). `private/` holds decoys that the privacy gate must never open
(matched by the default `archive.sensitive_path_regex`). File names avoid the substring
`exam` because that default regex has no word boundaries. Regenerate with
`python tests/fixtures/profile/build_fixtures.py`.
"""


def build_library() -> None:
    if LIBRARY.exists():
        shutil.rmtree(LIBRARY)
    LIBRARY.mkdir(parents=True)
    (LIBRARY / "_README.md").write_text(README, encoding="utf-8")
    (LIBRARY / "references.bib").write_text(BIB, encoding="utf-8")
    (LIBRARY / "zotero-export.json").write_text(
        json.dumps(CSL_JSON, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    (LIBRARY / "refs.ris").write_text(RIS, encoding="utf-8")
    notes = LIBRARY / "notes"
    notes.mkdir()
    for name, text in NOTES.items():
        (notes / name).write_text(text, encoding="utf-8")
    pdfs = LIBRARY / "pdfs"
    make_pdf(
        pdfs / "Sample-A-2019-Sieve-Bound.pdf",
        [
            "A sieve bound for twin prime pairs in short intervals",
            "Example Author A, Example Author B",
            "Example Journal of Number Theory 12 (2019) 1-20",
            "doi:10.0000/example.1",
            "Abstract. Fictional text for the Kallimachos test-suite.",
        ],
    )
    make_pdf(
        pdfs / "Sample-E-2024-Mean-Values.pdf",
        [
            "Mean values of fictional L-functions along vertical lines",
            "Example Author E",
            "arXiv:2402.00002v1 [math.NT] 2 Feb 2024",
            "Abstract. Fictional text for the Kallimachos test-suite.",
        ],
    )
    private = LIBRARY / "private"
    private.mkdir()
    (private / "students-grades.bib").write_text(PRIVATE_BIB, encoding="utf-8")
    (private / "personal-remarks.md").write_text(PRIVATE_NOTE, encoding="utf-8")
    make_pdf(
        private / "exam-2024.pdf",
        ["Final exam 2024", "Example Student", "doi:10.0000/private.3"],
    )


if __name__ == "__main__":
    build_library()
    build_http_fixtures()
    print(f"library: {LIBRARY}")
    print(f"fixtures: {HTTP} ({len(list(HTTP.glob('*.json')))} files)")
