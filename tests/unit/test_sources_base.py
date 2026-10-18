from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

from kallimachos.config.schema import Config
from kallimachos.interfaces import HttpClient, Record, Window
from kallimachos.profile.schema import Profile
from kallimachos.sources.base import (
    BaseSource,
    find_arxiv,
    normalize_doi,
    parse_datetime,
    parse_iso_date,
    run_source,
)
from kallimachos.sources.registry import enabled_sources
from tests.unit.test_sources_fakes import Fail, FakeHttp, example_profile, fixture, window


class Exploding(BaseSource):
    name = "exploding"

    def discover(self, window: Window, profile: Profile, http: HttpClient) -> Iterator[Record]:
        yield Record(title="partial")
        raise RuntimeError("parser bug")


def test_parse_iso_date_variants() -> None:
    assert parse_iso_date("2026") == dt.date(2026, 1, 1)
    assert parse_iso_date("2026-10") == dt.date(2026, 10, 1)
    assert parse_iso_date("2026-10-17") == dt.date(2026, 10, 17)
    assert parse_iso_date("2026-10-17T13:15:44Z") == dt.date(2026, 10, 17)
    assert parse_iso_date("2026/10/17") == dt.date(2026, 10, 17)
    assert parse_iso_date("2026-13-01") is None
    assert parse_iso_date("Oct 2026") is None
    assert parse_iso_date(None) is None


def test_parse_datetime_rfc822_and_iso() -> None:
    utc = dt.UTC
    assert parse_datetime("Sat, 17 Oct 2026 13:15:44 +0000") == dt.datetime(
        2026, 10, 17, 13, 15, 44, tzinfo=utc
    )
    assert parse_datetime("2026-10-17") == dt.datetime(2026, 10, 17, tzinfo=utc)
    assert parse_datetime("2026-10-17T01:00:00+03:00") == dt.datetime(2026, 10, 16, 22, tzinfo=utc)
    assert parse_datetime("garbage") is None


def test_identifier_helpers() -> None:
    assert normalize_doi("https://doi.org/10.0000/Example.1") == "10.0000/example.1"
    assert normalize_doi("doi:10.0000/x.") == "10.0000/x"
    assert normalize_doi("no doi") is None
    assert find_arxiv("https://arxiv.org/abs/2610.00001v3") == ("2610.00001", "v3")
    assert find_arxiv("https://arxiv.org/pdf/math/0601001v1") == ("math/0601001", "v1")
    assert find_arxiv("10.48550/arXiv.2610.00001") == ("2610.00001", None)


def test_crash_is_recorded_not_raised() -> None:
    run = run_source(
        Exploding(), window("2026-10-10T00:00", "2026-10-11T00:00"), example_profile(), FakeHttp()
    )
    assert run.stats.count == 1
    assert run.stats.errors == ["exploding: RuntimeError: parser bug"]


def test_one_failing_source_does_not_stop_the_others() -> None:
    cfg = Config()
    http = (
        FakeHttp()
        .add("https://export.arxiv.org/", Fail(503))
        .add("http://export.arxiv.org/", Fail(503))
        .add("https://api.crossref.org/", fixture("crossref", "journal_works"))
        .add("https://api.openalex.org/", fixture("openalex", "works_list"))
    )
    w = window("2026-10-10T00:00", "2026-10-11T00:00")
    runs = {s.name: run_source(s, w, example_profile(), http) for s in enabled_sources(cfg)}
    assert runs["arxiv"].stats.count == 0
    assert runs["arxiv"].stats.errors == [  # the real client's wording after its retries
        "export.arxiv.org: 3 tries failed",
        "export.arxiv.org: 3 tries failed",
    ]
    assert runs["crossref"].stats.count == 3 and runs["crossref"].stats.errors == []
    assert runs["openalex"].stats.count == 3 and runs["openalex"].stats.errors == []
    assert runs["arxiv"].stats.requests == 2
