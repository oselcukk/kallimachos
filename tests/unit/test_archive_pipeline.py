"""``archive run`` / ``archive retry`` flow with fakes."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest

from kallimachos.archive import download
from kallimachos.archive.catalog import Catalog, UnreachableList
from kallimachos.archive.download import HostPacer
from kallimachos.archive.pipeline import ArchiveContext, run_archive, run_retry
from kallimachos.archive.rows import ExistingEntry, write_rows
from kallimachos.config.schema import Config
from kallimachos.interfaces import (
    ArchiveTrigger,
    Author,
    CatalogEventType,
    CatalogStatus,
    OaCandidate,
    Record,
    WorkKind,
)
from kallimachos.paths import DataLayout
from kallimachos.profile.schema import AlwaysShowAuthor, Authors, Channel, Profile
from tests.unit.test_archive_support import FakeHttp, FakeSource, make_pdf, store_impl

__all__ = ["store_impl"]

NOW = dt.datetime(2026, 10, 18, 7, 0, tzinfo=dt.UTC)
ARXIV_ID = "2501.01234"
ARXIV_PDF = f"https://arxiv.org/pdf/{ARXIV_ID}v2"


@pytest.fixture(autouse=True)
def _pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download, "pdf_pages", lambda path: 3)


def preprint() -> Record:
    return Record(
        title="Fictional Results on Example Structures",
        authors=[Author("Example A. Author"), Author("Example B. Writer")],
        arxiv_id=f"{ARXIV_ID}v2",
        created=dt.date(2025, 1, 15),
        kind=WorkKind.PREPRINT,
    )


def profile() -> Profile:
    return Profile(
        channels=[
            Channel(id="A", name="Alpha", folder="Alpha-Topics"),
            Channel(id="B", name="Beta", folder="Beta-Topics"),
        ],
        authors=Authors(
            always_show=[AlwaysShowAuthor(name="Example Person Z", folder="Example-Person-Z")]
        ),
    )


def make_ctx(tmp_path: Path, **kw: object) -> ArchiveContext:
    cfg = Config()
    cfg.archive.enabled = True
    cfg.archive.root = str(tmp_path / "library")
    layout = DataLayout(tmp_path / "home")
    sources = kw.pop(
        "sources", {"arxiv": FakeSource("arxiv", records={f"arxiv:{ARXIV_ID}": preprint()})}
    )
    http = kw.pop("http", FakeHttp())
    now = [0.0]

    def sleep(s: float) -> None:
        now[0] += s

    return ArchiveContext(
        config=cfg,
        layout=layout,
        profile=profile(),
        http=http,  # type: ignore[arg-type]
        sources=sources,  # type: ignore[arg-type]
        main_channel=kw.pop("main_channel", lambda r, p, w, c: "A"),  # type: ignore[arg-type]
        pacer=HostPacer(clock=lambda: now[0], sleep=sleep),
        now=lambda: NOW,
        **kw,  # type: ignore[arg-type]
    )


def manual(ident: str, **kw: object) -> ArchiveTrigger:
    return ArchiveTrigger(kind="manual", ident=ident, **kw)  # type: ignore[arg-type]


def test_run_stores_pdf_and_catalog(tmp_path: Path, store_impl: None) -> None:
    body = make_pdf("fictional")
    http = FakeHttp()
    http.add(ARXIV_PDF, body)
    ctx = make_ctx(tmp_path, http=http)
    report = run_archive(ctx, [manual(f"arxiv:{ARXIV_ID}", note="manual:1")])
    assert report.stored == 1 and not report.errors
    name = "Author-Writer_2025_Fictional-Results-on-Example-Structures_arXiv-2501.01234v2.pdf"
    path = tmp_path / "library" / "Alpha-Topics" / name
    assert path.read_bytes() == body
    cat = Catalog.load(ctx.layout.catalog_file)
    (entry,) = cat.entries()
    sha = hashlib.sha256(body).hexdigest()
    assert entry.id == sha[:12] and entry.sha256 == sha
    assert entry.path == f"Alpha-Topics/{name}" and entry.filename == name
    assert entry.arxiv == ARXIV_ID and entry.arxiv_version == "v2"
    assert entry.channel == "A" and entry.status is CatalogStatus.STORED
    assert entry.oa_source == "arxiv" and entry.oa_url == ARXIV_PDF
    assert [e.type for e in entry.events] == [CatalogEventType.STORED]
    assert entry.triggers == ["manual:1"]
    log = [json.loads(x) for x in ctx.layout.archive_log_file.read_text().splitlines()]
    assert log[-1]["counts"]["stored"] == 1
    assert report.summary_line() == "archive: 1 stored"
    assert not (ctx.layout.archive / "tmp").exists()

    # second run: same key is seen, no request
    calls = len(http.calls)
    again = run_archive(ctx, [manual(f"arxiv:{ARXIV_ID}v3")])
    assert again.skipped_seen == 1 and len(http.calls) == calls
    assert again.summary_line() == ""


def test_dry_run_writes_nothing_and_requests_nothing(tmp_path: Path, store_impl: None) -> None:
    http = FakeHttp()
    http.add(ARXIV_PDF, make_pdf())
    ctx = make_ctx(tmp_path, http=http)
    report = run_archive(ctx, [manual(f"arxiv:{ARXIV_ID}")], dry_run=True)
    assert report.planned == [f"manual arxiv:{ARXIV_ID}"]
    assert http.calls == []
    assert not (tmp_path / "home").exists()
    assert not (tmp_path / "library").exists()


def test_unreachable_on_forbidden_and_html(tmp_path: Path, store_impl: None) -> None:
    rec = Record(
        title="Closed Fictional Article", doi="10.0000/example.1", authors=[Author("Ex One")]
    )
    oa_src = FakeSource(
        "openalex",
        records={"doi:10.0000/example.1": rec},
        candidates=[
            OaCandidate(url="https://repo.example.org/a.pdf", source="openalex"),
            OaCandidate(url="https://repo2.example.org/b.pdf", source="openalex"),
            OaCandidate(url="https://journal.example.org/landing", source="openalex", is_pdf=False),
        ],
    )
    http = FakeHttp()
    http.add("https://repo.example.org/a.pdf", "denied", status=403, ctype="text/html")
    http.add("https://repo2.example.org/b.pdf", "<html>login</html>", ctype="text/html")
    ctx = make_ctx(tmp_path, http=http, sources={"openalex": oa_src})
    report = run_archive(ctx, [manual("10.0000/example.1")])
    assert report.unreachable == 1 and report.stored == 0
    (u,) = UnreachableList.load(ctx.layout.unreachable_file).entries()
    assert u.status == "open" and u.title == "Closed Fictional Article"
    assert "forbidden" in u.reason and "html" in u.reason
    assert u.oa_tried == ["https://repo.example.org/a.pdf", "https://repo2.example.org/b.pdf"]
    assert u.landing == "https://journal.example.org/landing"
    assert u.doi_url == "https://doi.org/10.0000/example.1"
    assert "Closed Fictional Article" in (ctx.layout.archive / "Unreachable.md").read_text()
    assert not (tmp_path / "library").exists()

    # retry leaves 403/HTML entries alone
    calls = len(http.calls)
    retry = run_retry(ctx)
    assert retry.retry_skipped == 1 and retry.retried == 0 and len(http.calls) == calls


def test_retry_429_spacing_and_success(tmp_path: Path, store_impl: None) -> None:
    rec = Record(title="Rate Limited Fictional", doi="10.0000/example.4")
    oa_src = FakeSource(
        "openalex",
        records={"doi:10.0000/example.4": rec},
        candidates=[OaCandidate(url="https://slow.example.org/x.pdf", source="openalex")],
    )
    http = FakeHttp()
    http.add("https://slow.example.org/x.pdf", "", status=429, ctype="text/plain")
    ctx = make_ctx(tmp_path, http=http, sources={"openalex": oa_src})
    run_archive(ctx, [manual("10.0000/example.4")])
    (u,) = UnreachableList.load(ctx.layout.unreachable_file).entries()
    assert "rate_limited" in u.reason
    # host now serves the PDF; retry downloads and closes the entry
    http.add("https://slow.example.org/x.pdf", make_pdf("ok"))
    ctx2 = make_ctx(tmp_path, http=http, sources={"openalex": oa_src})
    report = run_retry(ctx2)
    assert report.retried == 1 and report.stored == 1
    (u2,) = UnreachableList.load(ctx.layout.unreachable_file).entries()
    assert u2.status == "closed" and u2.closed_by == "downloaded"
    assert [r.result for r in u2.retries] == ["stored"]


def test_retry_drops_host_after_two_consecutive_429(tmp_path: Path, store_impl: None) -> None:
    recs = {
        f"doi:10.0000/example.{i}": Record(title=f"Fictional {i}", doi=f"10.0000/example.{i}")
        for i in (5, 6, 7)
    }
    src = FakeSource(
        "openalex",
        records=recs,
        candidates=[OaCandidate(url="https://slow.example.org/x.pdf", source="openalex")],
    )
    http = FakeHttp()
    http.add("https://slow.example.org/x.pdf", "", status=429, ctype="text/plain")
    ctx = make_ctx(tmp_path, http=http, sources={"openalex": src})
    run_archive(ctx, [manual(k) for k in recs])
    ctx2 = make_ctx(tmp_path, http=http, sources={"openalex": src})
    before = len(http.calls)
    run_retry(ctx2)
    assert http.calls[before:].count("https://slow.example.org/x.pdf") == 2
    assert ctx2.pacer.dropped == {"slow.example.org"}


def test_sha_duplicate_is_not_stored_twice(tmp_path: Path, store_impl: None) -> None:
    body = make_pdf("same")
    a = Record(title="First Fictional", doi="10.0000/a")
    b = Record(title="Second Fictional", doi="10.0000/b")
    src = FakeSource(
        "openalex",
        records={"doi:10.0000/a": a, "doi:10.0000/b": b},
        candidates=[OaCandidate(url="https://repo.example.org/same.pdf", source="openalex")],
    )
    http = FakeHttp()
    http.add("https://repo.example.org/same.pdf", body)
    ctx = make_ctx(tmp_path, http=http, sources={"openalex": src})
    report = run_archive(ctx, [manual("10.0000/a"), manual("10.0000/b")])
    assert report.stored == 1 and report.duplicate == 1
    assert len(list((tmp_path / "library").rglob("*.pdf"))) == 1
    assert (
        not list((ctx.layout.archive / "tmp").glob("*"))
        if (ctx.layout.archive / "tmp").exists()
        else True
    )


def test_existing_library_match_skips_download(tmp_path: Path, store_impl: None) -> None:
    ctx = make_ctx(tmp_path)
    sha = hashlib.sha256(b"old").hexdigest()
    write_rows(
        ctx.layout.existing_catalog_file,
        [
            ExistingEntry(
                path="papers/x.pdf",
                root="/lib",
                size=3,
                mtime=1.0,
                sha256=sha,
                arxiv=ARXIV_ID,
                keys=[f"arxiv:{ARXIV_ID}"],
            )
        ],
    )
    report = run_archive(ctx, [manual(ARXIV_ID)])
    assert report.existing == 1 and report.stored == 0
    (entry,) = Catalog.load(ctx.layout.catalog_file).entries()
    assert entry.status is CatalogStatus.EXISTING and entry.id == sha[:12]
    assert entry.events[0].type is CatalogEventType.EXISTING
    assert not (tmp_path / "library").exists()


def test_preprint_then_journal_supersedes_without_deleting(
    tmp_path: Path, store_impl: None
) -> None:
    http = FakeHttp()
    http.add(ARXIV_PDF, make_pdf("preprint"))
    ctx = make_ctx(tmp_path, http=http)
    run_archive(ctx, [manual(ARXIV_ID)])
    journal = preprint()
    journal.doi = "10.0000/example.10"
    journal.published = dt.date(2026, 5, 1)
    journal.venue = "Fictional Journal"
    journal.kind = WorkKind.ARTICLE
    src = {
        "openalex": FakeSource(
            "openalex",
            records={"doi:10.0000/example.10": journal},
            candidates=[OaCandidate(url="https://repo.example.org/journal.pdf", source="openalex")],
        ),
        "arxiv": FakeSource("arxiv", records={f"arxiv:{ARXIV_ID}": preprint()}),
    }
    http.add("https://repo.example.org/journal.pdf", make_pdf("journal"))
    ctx2 = make_ctx(tmp_path, http=http, sources=src)
    report = run_archive(ctx2, [manual("10.0000/example.10")])
    assert report.stored == 1 and len(report.superseded) == 1
    entries = Catalog.load(ctx.layout.catalog_file).entries()
    old = next(e for e in entries if e.doi is None)
    new = next(e for e in entries if e.doi == "10.0000/example.10")
    assert (
        old.events[-1].type is CatalogEventType.SUPERSEDED and old.events[-1].detail["by"] == new.id
    )
    assert old.status is CatalogStatus.STORED  # nothing removed
    assert (tmp_path / "library" / old.path).exists()  # type: ignore[operator]
    assert new.filename and new.filename.startswith("Author-Writer_2026_")


def test_filter_and_exemption(tmp_path: Path, store_impl: None) -> None:
    http = FakeHttp()
    http.add(ARXIV_PDF, make_pdf())
    ctx = make_ctx(tmp_path, http=http, keep=lambda r, t, c: "off-topic")
    auto = ArchiveTrigger(kind="auto_pass", ident=f"arxiv:{ARXIV_ID}", run_id="2026-10-18-daily")
    report = run_archive(ctx, [auto])
    assert report.filtered == 1 and report.stored == 0
    rows = [json.loads(x) for x in ctx.layout.processed_file.read_text().splitlines()]
    assert rows[0]["status"] == "filtered" and rows[0]["reason"] == "off-topic"
    # the same work requested explicitly is exempt from the filter and from processed.jsonl
    report2 = run_archive(ctx, [manual(ARXIV_ID)])
    assert report2.stored == 1


def test_folders_person_and_unsorted(tmp_path: Path, store_impl: None) -> None:
    rec_person = Record(title="Person Paper", doi="10.0000/p", authors=[Author("example person z")])
    rec_none = Record(title="Nobody Paper", doi="10.0000/n")
    src = FakeSource(
        "openalex",
        records={"doi:10.0000/p": rec_person, "doi:10.0000/n": rec_none},
    )
    http = FakeHttp()
    pdfs = iter([make_pdf("p"), make_pdf("n")])

    def oa_for(record: Record, _http: object) -> list[OaCandidate]:
        url = f"https://repo.example.org/{record.doi}.pdf"
        http.add(url, next(pdfs))
        return [OaCandidate(url=url, source="openalex")]

    src.oa_pdf_candidates = oa_for  # type: ignore[method-assign]
    ctx = make_ctx(
        tmp_path, http=http, sources={"openalex": src}, main_channel=lambda r, p, w, c: None
    )
    run_archive(ctx, [manual("10.0000/p"), manual("10.0000/n")])
    folders = sorted(e.folder or "" for e in Catalog.load(ctx.layout.catalog_file).entries())
    assert folders == ["_Unsorted", "persons/Example-Person-Z"]


def test_no_metadata_is_an_error_not_unreachable(tmp_path: Path, store_impl: None) -> None:
    ctx = make_ctx(tmp_path, sources={})
    report = run_archive(ctx, [manual("10.0000/unknown")])
    assert report.no_metadata == 1 and report.errors
    assert UnreachableList.load(ctx.layout.unreachable_file).entries() == []


def test_abs_page_fallback(tmp_path: Path, fixtures_dir: Path, store_impl: None) -> None:
    http = FakeHttp()
    http.add(
        f"https://arxiv.org/abs/{ARXIV_ID}",
        (fixtures_dir / "archive" / "arxiv_abs.html").read_bytes(),
        ctype="text/html",
    )
    http.add(f"https://arxiv.org/pdf/{ARXIV_ID}", make_pdf())
    ctx = make_ctx(tmp_path, http=http, sources={})
    report = run_archive(ctx, [manual(ARXIV_ID)])
    assert report.stored == 1
    (entry,) = Catalog.load(ctx.layout.catalog_file).entries()
    assert entry.title == "Fictional Results on Example Structures"
    assert entry.authors == ["Author, Example A.", "Writer, Example B."]
    assert (
        entry.filename
        == "Author-Writer_2025_Fictional-Results-on-Example-Structures_arXiv-2501.01234.pdf"
    )
