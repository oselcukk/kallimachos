"""Download, O_EXCL placement, host pacing, catalog and OA order."""

from __future__ import annotations

import datetime as dt
import hashlib
import shutil
from pathlib import Path

import pytest

from kallimachos.archive import download, oa
from kallimachos.archive.catalog import Catalog, UnreachableList, eid_from_sha
from kallimachos.archive.download import DownloadError, HostPacer, download_pdf, write_exclusive
from kallimachos.config.schema import Config
from kallimachos.interfaces import (
    CatalogEntry,
    CatalogEvent,
    CatalogEventType,
    CatalogStatus,
    OaCandidate,
    Record,
    SchemaViolation,
    UnreachableEntry,
)
from tests.unit.test_archive_support import FakeHttp, FakeSource, make_pdf, store_impl

__all__ = ["store_impl"]

URL = "https://arxiv.org/pdf/2501.01234v2"


@pytest.fixture(autouse=True)
def _pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download, "pdf_pages", lambda path: 1)


def test_download_ok(tmp_path: Path) -> None:
    body = make_pdf("hello")
    http = FakeHttp()
    http.add(URL, body)
    res = download_pdf(
        OaCandidate(url=URL, source="arxiv", version="v2"), tmp_path / "t.pdf", http, max_mb=150
    )
    assert res.sha256 == hashlib.sha256(body).hexdigest()
    assert res.size == len(body) and res.pages == 1
    assert (tmp_path / "t.pdf").read_bytes() == body


@pytest.mark.parametrize(
    ("status", "body", "ctype", "kind"),
    [
        (403, b"denied", "text/html", "forbidden"),
        (429, b"", "text/plain", "rate_limited"),
        (503, b"", "text/plain", "server"),
        (200, b"<html><body>login</body></html>", "text/html", "html"),
        (200, b"PK\x03\x04zip", "application/zip", "not_pdf"),
    ],
)
def test_download_rejections(
    tmp_path: Path, status: int, body: bytes, ctype: str, kind: str
) -> None:
    http = FakeHttp()
    http.add(URL, body, status=status, ctype=ctype)
    with pytest.raises(DownloadError) as exc:
        download_pdf(OaCandidate(url=URL, source="arxiv"), tmp_path / "t.pdf", http, max_mb=150)
    assert exc.value.kind == kind
    assert not (tmp_path / "t.pdf").exists()
    assert exc.value.retryable == (kind in {"rate_limited", "server"})


def test_download_size_cap(tmp_path: Path) -> None:
    http = FakeHttp()
    http.add(URL, b"%PDF-" + b"0" * (1024 * 1024 + 10))
    with pytest.raises(DownloadError) as exc:
        download_pdf(OaCandidate(url=URL, source="arxiv"), tmp_path / "t.pdf", http, max_mb=1)
    assert exc.value.kind == "too_large"


def test_download_zero_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download, "pdf_pages", lambda path: 0)
    http = FakeHttp()
    http.add(URL, make_pdf())
    with pytest.raises(DownloadError):
        download_pdf(OaCandidate(url=URL, source="arxiv"), tmp_path / "t.pdf", http, max_mb=150)
    assert not (tmp_path / "t.pdf").exists()


def test_download_never_overwrites_destination(tmp_path: Path) -> None:
    dest = tmp_path / "t.pdf"
    dest.write_bytes(b"user data")
    http = FakeHttp()
    http.add(URL, make_pdf())
    with pytest.raises(FileExistsError):
        download_pdf(OaCandidate(url=URL, source="arxiv"), dest, http, max_mb=150)
    assert dest.read_bytes() == b"user data"


@pytest.mark.skipif(shutil.which("pdfinfo") is None, reason="pdfinfo not installed")
def test_pdf_pages_with_pdfinfo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    p = tmp_path / "x.pdf"
    p.write_bytes(make_pdf())
    assert download.pdf_pages(p) == 1


def test_write_exclusive_new_same_and_clash(tmp_path: Path) -> None:
    target = tmp_path / "lib" / "Name_2025_Title.pdf"
    tmp1 = tmp_path / "a.tmp"
    tmp1.write_bytes(b"%PDF-one")
    assert write_exclusive(tmp1, target) == target
    assert not tmp1.exists()
    # identical content: reused, nothing new
    tmp2 = tmp_path / "b.tmp"
    tmp2.write_bytes(b"%PDF-one")
    assert write_exclusive(tmp2, target) == target
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]
    # different content: _<sha8>, original untouched
    tmp3 = tmp_path / "c.tmp"
    tmp3.write_bytes(b"%PDF-two")
    sha8 = hashlib.sha256(b"%PDF-two").hexdigest()[:8]
    placed = write_exclusive(tmp3, target)
    assert placed.name == f"Name_2025_Title_{sha8}.pdf"
    assert target.read_bytes() == b"%PDF-one"
    assert placed.read_bytes() == b"%PDF-two"


def test_write_exclusive_refuses_when_both_names_taken(tmp_path: Path) -> None:
    target = tmp_path / "x.pdf"
    target.write_bytes(b"user 1")
    sha8 = hashlib.sha256(b"%PDF-new").hexdigest()[:8]
    (tmp_path / f"x_{sha8}.pdf").write_bytes(b"user 2")
    tmp = tmp_path / "n.tmp"
    tmp.write_bytes(b"%PDF-new")
    with pytest.raises(FileExistsError):
        write_exclusive(tmp, target)
    assert target.read_bytes() == b"user 1"


def test_host_pacer_intervals_and_429_policy() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    pacer = HostPacer(clock=lambda: now[0], sleep=sleep)
    pacer.wait("https://arxiv.org/pdf/1")
    pacer.wait("https://arxiv.org/pdf/2")
    assert slept == [pytest.approx(3.2)]
    slept.clear()
    host = "https://ams.example.org/a.pdf"
    pacer.wait(host)
    pacer.record(host, 429)
    assert pacer.allowed(host)
    pacer.wait(host)
    assert slept and slept[-1] >= 20.0
    pacer.record(host, 429)
    assert not pacer.allowed(host)
    other = "https://b.example.org/x.pdf"
    pacer.record(other, 429)
    pacer.record(other, 200)
    pacer.record(other, 429)
    assert pacer.allowed(other)  # not consecutive


# --------------------------------------------------------------------------- catalog


def _entry(sha: str, **kw: object) -> CatalogEntry:
    at = dt.datetime(2026, 10, 18, tzinfo=dt.UTC)
    return CatalogEntry(
        id=eid_from_sha(sha),
        sha256=sha,
        keys=["doi:10.0000/example.1"],
        title="Fictional",
        events=[CatalogEvent(at=at, type=CatalogEventType.STORED)],
        **kw,  # type: ignore[arg-type]
    )


def test_catalog_roundtrip_and_lookup(tmp_path: Path, store_impl: None) -> None:
    sha = hashlib.sha256(b"x").hexdigest()
    cat = Catalog(tmp_path / "catalog.jsonl")
    cat.add(_entry(sha, arxiv="2501.01234", arxiv_version="v2"))
    with pytest.raises(ValueError):
        cat.add(_entry(sha))
    cat.add_event(
        sha[:12],
        CatalogEvent(at=dt.datetime(2026, 10, 19, tzinfo=dt.UTC), type=CatalogEventType.SUPERSEDED),
    )
    cat.save()
    loaded = Catalog.load(tmp_path / "catalog.jsonl")
    e = loaded.get(sha[:12])
    assert e is not None and len(e.events) == 2 and e.status is CatalogStatus.STORED
    assert loaded.find_by_sha(sha) is e
    assert loaded.find_by_keys(["arxiv:2501.01234v7"]) is e
    assert loaded.find_by_keys(["DOI:10.0000/EXAMPLE.1".lower()]) is e
    assert loaded.find_by_keys(["doi:10.0000/other"]) is None
    assert eid_from_sha(sha) == sha[:12]


def test_catalog_save_validates_schema(tmp_path: Path, store_impl: None) -> None:
    cat = Catalog(tmp_path / "catalog.jsonl")
    cat.add(_entry("not-a-sha"))
    with pytest.raises(SchemaViolation):
        cat.save()
    assert not (tmp_path / "catalog.jsonl").exists()


def test_unreachable_upsert_close(tmp_path: Path, store_impl: None) -> None:
    ul = UnreachableList(tmp_path / "u.jsonl")
    ul.upsert(UnreachableEntry(id="doi:10.0000/x", keys=["doi:10.0000/x"], reason="a"))
    ul.upsert(UnreachableEntry(id="doi:10.0000/x", keys=["doi:10.0000/x"], reason="b"))
    assert len(ul.entries()) == 1 and ul.entries()[0].reason == "b"
    assert ul.open_keys() == {"doi:10.0000/x"}
    assert ul.close(["doi:10.0000/x"], "downloaded")
    ul.save()
    again = UnreachableList.load(tmp_path / "u.jsonl")
    assert again.entries()[0].status == "closed" and again.entries()[0].closed_by == "downloaded"


# --------------------------------------------------------------------------- OA order


def test_oa_order_and_only_open_routes(fixtures_dir: Path) -> None:
    rec = Record(title="t", doi="10.0000/example.1", arxiv_id="2501.01234v2", eprint_id="2024/123")
    sources = {
        "openalex": FakeSource(
            "openalex",
            candidates=[
                OaCandidate(url="https://repo.example.org/a.pdf", source="openalex"),
                OaCandidate(
                    url="https://journal.example.org/landing", source="openalex", is_pdf=False
                ),
            ],
        ),
        "crossref": FakeSource(
            "crossref",
            candidates=[
                OaCandidate(url="https://pub.example.org/paywalled.pdf", source="crossref")
            ],
        ),
    }
    http = FakeHttp()
    out = oa.oa_candidates(rec, sources, http, Config())  # type: ignore[arg-type]
    assert [c.url for c in out] == [
        "https://arxiv.org/pdf/2501.01234v2",
        "https://eprint.iacr.org/2024/123.pdf",
        "https://repo.example.org/a.pdf",
        "https://journal.example.org/landing",
    ]
    assert out[0].version == "v2"
    assert sources["crossref"].oa_calls == 0  # crossref is not an OA route
    assert http.calls == []  # no Unpaywall without email_ref


def test_oa_source_order_is_enforced() -> None:
    shuffled = [
        OaCandidate(url="https://u.example.org/1.pdf", source="unpaywall"),
        OaCandidate(url="https://o.example.org/1.pdf", source="openalex"),
        OaCandidate(url="https://arxiv.org/pdf/1", source="arxiv"),
        OaCandidate(url="https://arxiv.org/pdf/1", source="arxiv"),
        OaCandidate(url="https://scihub.example/1.pdf", source="mirror"),
    ]
    assert [c.source for c in oa.order_candidates(shuffled)] == ["arxiv", "openalex", "unpaywall"]


def test_unpaywall_only_with_email(fixtures_dir: Path) -> None:
    rec = Record(title="t", doi="10.0000/example.1")
    http = FakeHttp()
    http.add(
        "https://api.unpaywall.org/v2/10.0000/example.1",
        (fixtures_dir / "archive" / "unpaywall.json").read_bytes(),
        ctype="application/json",
    )
    cfg = Config()
    assert oa.oa_candidates(rec, {}, http, cfg, resolve_email=lambda ref: "user@example.org") == []
    cfg.sources.unpaywall.email_ref = "env:EXAMPLE_EMAIL"
    out = oa.oa_candidates(rec, {}, http, cfg, resolve_email=lambda ref: "user@example.org")
    assert [(c.url, c.is_pdf) for c in out] == [
        ("https://repository.example.org/example1.pdf", True),
        ("https://journal.example.org/article/1", False),
    ]
