"""Profile: files -> profile pipeline, privacy gate, journals and proposals."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from kallimachos.config.schema import DEFAULT_SENSITIVE_PATH_REGEX, Config, ProfileBuildConfig
from kallimachos.interfaces import Record, WorkKind
from kallimachos.profile.from_files import (
    build_profile,
    collect,
    dedupe,
    is_lecture_note_like,
    is_person_name_like,
    ngrams,
    passes_privacy_gate,
    propose_channels,
)
from kallimachos.profile.importers import ImportedRef
from kallimachos.profile.journals import propose_journals, yearly_volume
from kallimachos.profile.offline import FixtureHttp

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "e2e" / "sample_library"
FIXTURES = ROOT / "fixtures" / "profile" / "http"
SENSITIVE = [re.compile(DEFAULT_SENSITIVE_PATH_REGEX)]
TODAY = dt.date(2026, 10, 18)  # fixtures record yearly volumes for 2025


# ---------------------------------------------------------------------------
# collect / dedupe
# ---------------------------------------------------------------------------


def test_collect_never_opens_sensitive_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    for name in ("read_text", "read_bytes"):
        original = getattr(Path, name)

        def spy(self: Path, *args: object, _orig=original, **kwargs: object) -> object:  # type: ignore[no-untyped-def]
            opened.append(self.as_posix())
            return _orig(self, *args, **kwargs)

        monkeypatch.setattr(Path, name, spy)
    refs, skipped = collect([LIBRARY], sensitive_patterns=SENSITIVE, pdf_tool="builtin")
    assert {Path(s).name for s in skipped} == {
        "exam-2024.pdf",
        "personal-remarks.md",
        "students-grades.bib",
    }
    assert not any("private" in Path(p).relative_to(LIBRARY).parts for p in opened)
    assert not any("private" in r.source_file for r in refs)
    assert not any((r.doi or "").startswith("10.0000/private") for r in refs)
    assert all(not Path(r.source_file).name.startswith("_") for r in refs)
    assert len(refs) >= 12


def test_collect_single_sensitive_file_is_skipped(tmp_path: Path) -> None:
    secret = tmp_path / "salary-list.bib"
    secret.write_text("@article{x, title={Hidden salary data title}}", encoding="utf-8")
    refs, skipped = collect([secret], sensitive_patterns=SENSITIVE)
    assert refs == [] and skipped == [str(secret)]
    # the user-given root is judged by its own name, not by its parents (/private/tmp on macOS)
    root = tmp_path / "plain-root"
    root.mkdir()
    (root / "ok.bib").write_text("@article{x, title={A plain fictional title}}", encoding="utf-8")
    refs, skipped = collect([root], sensitive_patterns=[re.compile("(?i)pytest|private")])
    assert [r.title for r in refs] == ["A plain fictional title"] and skipped == []


def test_dedupe_merges_sources_and_fills_fields() -> None:
    a = ImportedRef(
        title="A sieve bound for twin prime pairs in short intervals",
        source_file="n.md",
        authors=["Example Author A"],
        project="sieves",
    )
    b = ImportedRef(
        title="A Sieve Bound for Twin Prime Pairs in Short Intervals",
        source_file="r.bib",
        authors=["Example Author A", "Example Author B"],
        year=2019,
        doi="10.0000/example.1",
        kind=WorkKind.ARTICLE,
        tags=["sieve"],
    )
    c = ImportedRef(title="Unrelated fictional title", source_file="z.ris")
    out = dedupe([a, b, c])
    assert len(out) == 2
    merged = out[0]
    assert merged.source_files == ["n.md", "r.bib"]
    assert merged.doi == "10.0000/example.1" and merged.year == 2019
    assert merged.kind is WorkKind.ARTICLE and merged.tags == ["sieve"]
    assert merged.project == "sieves" and merged.authors == ["Example Author A"]


# ---------------------------------------------------------------------------
# privacy gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "allowed"),
    [
        (ImportedRef(title="Short", source_file="x", doi="10.0000/example.1"), True),
        (ImportedRef(title="x", source_file="x", arxiv="2301.00001"), True),
        (
            ImportedRef(
                title="Zero density estimates for a family of Dirichlet series",
                source_file="x",
                kind=WorkKind.ARTICLE,
            ),
            True,
        ),
        (
            ImportedRef(
                title="Zero density estimates for a family of Dirichlet series", source_file="x"
            ),
            False,
        ),
        (
            ImportedRef(
                title="Introduction to fictional sieve methods", source_file="x", kind=WorkKind.BOOK
            ),
            False,
        ),
        (
            ImportedRef(
                title="A fairly long fictional book title about sieve methods",
                source_file="x",
                kind=WorkKind.BOOK,
            ),
            True,
        ),
        (
            ImportedRef(
                title="Lecture notes on sieve theory for the fictional course",
                source_file="x",
                kind=WorkKind.ARTICLE,
            ),
            False,
        ),
        (
            ImportedRef(
                title="Math 210B. Group cohomology and group extensions",
                source_file="x",
                kind=WorkKind.ARTICLE,
            ),
            False,
        ),
        (ImportedRef(title="Jane Q. Sample", source_file="x", kind=WorkKind.ARTICLE), False),
        (ImportedRef(title="Jane Q. Sample", source_file="x", doi="10.0000/example.2"), True),
        (
            ImportedRef(
                title="Homework 3 solutions for the fictional seminar group",
                source_file="x",
                doi="10.0000/example.3",
            ),
            True,  # only the DOI leaves the machine
        ),
    ],
)
def test_privacy_gate(ref: ImportedRef, allowed: bool) -> None:
    assert passes_privacy_gate(ref) is allowed


def test_lecture_and_person_detectors() -> None:
    assert is_lecture_note_like("Chapter 6. Cohomology and group extensions")
    assert not is_lecture_note_like("Notes on the distribution of fictional primes")
    assert is_person_name_like("Ada Example-Lovelace")
    assert not is_person_name_like("On ideal lattices and learning with errors over rings")


def test_private_library_produces_no_request() -> None:
    http = FixtureHttp([FIXTURES])
    draft = build_profile([LIBRARY / "private"], Config(), http, today=TODAY)
    assert http.requests == []
    assert draft.requests == []
    assert draft.profile.seeds == []
    assert [Path(p).name for p in draft.skipped_private] == ["private"]  # root skipped whole


def test_person_and_lecture_refs_produce_no_request(tmp_path: Path) -> None:
    (tmp_path / "Jane-Q-Sample.md").write_text("# Jane Q. Sample\n", encoding="utf-8")
    (tmp_path / "lecture.bib").write_text(
        "@article{a, title={Lecture notes on fictional analytic number theory topics},\n"
        "  author={X}}\n"
        "@misc{b, title={A short fictional reading list for students}, author={Y}}\n",
        encoding="utf-8",
    )
    http = FixtureHttp([FIXTURES])
    draft = build_profile([tmp_path], Config(), http, today=TODAY)
    assert http.requests == []
    assert len(draft.not_queried) == 3 and draft.unmatched == []
    assert draft.profile.seeds == []
    assert "Not queried" in draft.report_md


# ---------------------------------------------------------------------------
# full pipeline on the sample library (fixtures replayed, no network)
# ---------------------------------------------------------------------------


def test_build_profile_sample_library() -> None:
    http = FixtureHttp([FIXTURES])
    draft = build_profile([LIBRARY], Config(), http, pdf_tool="builtin", today=TODAY)
    profile = draft.profile
    assert profile.status == "draft"
    assert len(profile.seeds) >= 2
    assert len(profile.channels) >= 1
    journals = [j for ch in profile.channels for j in ch.journals]
    assert len(journals) >= 1
    by_name = {j.name: j for j in journals}
    assert by_name["Example Journal of Number Theory"].mode == "full"
    assert by_name["Example Journal of Number Theory"].yearly_volume == 120
    assert by_name["Example Mathematical Proceedings"].mode == "topic_filtered"
    assert by_name["Example Mathematical Proceedings"].yearly_volume == 800
    assert all(not j.verified for j in journals)
    seeds_by_id = {s.openalex_id: s for s in profile.seeds}
    assert seeds_by_id["W1000000001"].confidence == "high"
    assert seeds_by_id["W1000000001"].first_author == "A"
    assert sorted(Path(f).name for f in seeds_by_id["W1000000001"].source_files) == [
        "A-2019-Sieve-Bound-Twin-Primes.md",
        "Sample-A-2019-Sieve-Bound.pdf",
        "references.bib",
    ]
    assert seeds_by_id["W1000000009"].doi is None  # matched via autocomplete
    assert seeds_by_id["W1000000010"].doi == "10.0000/example.10"  # matched via Crossref
    assert {f.openalex_id: f.cited_by_seeds for f in profile.foundational} == {
        "W2000000002": 5,
        "W2000000001": 3,
        "W2000000003": 3,
    }
    assert profile.foundational[0].title == "A fictional survey of exponential sums"
    follow = {a.name for a in profile.authors.follow}
    assert {"Example Author A", "Example Author B", "Example Author C"} <= follow
    assert all(a.status == "candidate" for a in profile.authors.follow)
    # channel A comes from the notes' ``project: sieves`` tag
    assert profile.channels[0].name == "sieves" and profile.channels[0].folder == "A-Sieves"
    assert all(len(ch.keywords.core) > 0 for ch in profile.channels)
    assert "private" not in " ".join(draft.requests)
    assert {Path(r.source_file).name for r in draft.not_queried} == {
        "Jane-Q-Sample.md",
        "Reading-Plan-Fictional-Seminar.md",
        "references.bib",
    }
    assert "## Seeds" in draft.report_md and "Skipped paths" in draft.report_md


def test_build_profile_without_fixtures_is_local_only() -> None:
    http = FixtureHttp()
    draft = build_profile([LIBRARY / "references.bib"], Config(), http, today=TODAY)
    assert draft.matches == []
    assert all(s.confidence == "low" for s in draft.profile.seeds)
    assert {s.doi or s.arxiv for s in draft.profile.seeds} == {
        "10.0000/example.1",
        "10.0000/example.2",
        "2301.00001",
        "10.0000/example.5",
    }
    assert any("local-only" in w for w in draft.warnings)
    journals = [j for ch in draft.profile.channels for j in ch.journals]
    assert {j.name for j in journals} == {
        "Example Journal of Number Theory",
        "Example Mathematical Proceedings",
    }
    assert all(j.mode == "topic_filtered" and j.yearly_volume is None for j in journals)


# ---------------------------------------------------------------------------
# journals and proposals
# ---------------------------------------------------------------------------


def test_yearly_volume_and_mode_threshold() -> None:
    http = FixtureHttp([FIXTURES])
    assert yearly_volume("https://openalex.org/S9000000001", 2025, http) == 120
    assert yearly_volume("S9000000002", 2025, http) == 800
    assert yearly_volume("S0", 2025, http) is None
    seeds = [
        Record(
            title="t1",
            venue="Example Journal of Number Theory",
            issn=["0000-0001"],
            kind=WorkKind.ARTICLE,
        ),
        Record(title="t2", venue="Example Mathematical Proceedings", kind=WorkKind.ARTICLE),
        Record(title="t3", venue="arXiv (Example Mirror)", kind=WorkKind.PREPRINT),
        Record(title="t4", venue="Example University Press", kind=WorkKind.BOOK),
    ]
    ids = {
        "Example Journal of Number Theory": "S9000000001",
        "Example Mathematical Proceedings": "S9000000002",
    }
    journals = propose_journals(seeds, http, ProfileBuildConfig(), source_ids=ids, year=2025)
    assert [(j.name, j.mode, j.yearly_volume) for j in journals] == [
        ("Example Journal of Number Theory", "full", 120),
        ("Example Mathematical Proceedings", "topic_filtered", 800),
    ]
    strict = propose_journals(
        seeds,
        http,
        ProfileBuildConfig(journal_full_max_yearly_volume=100),
        source_ids=ids,
        year=2025,
    )
    assert all(j.mode == "topic_filtered" for j in strict)


def test_ngrams_and_channel_fallback() -> None:
    counts = ngrams(
        ["Large sieve inequalities for character sums", "The large sieve and character sums"]
    )
    assert counts["large sieve"] == 2 and counts["character sums"] == 2
    assert "sieve and" not in counts  # edge stopword dropped
    refs = [ImportedRef(title="Only local fictional reference about graphs", source_file="a")]
    channels, assignment = propose_channels([], refs)
    assert [c.id for c in channels] == ["A"] and channels[0].name == "Imported library"
    assert assignment[id(refs[0])] == "A"
    assert re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$", channels[0].folder)
