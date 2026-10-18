"""tests/unit/fakes.py: FixtureHttp behaves like the real client."""

from __future__ import annotations

import pytest

from kallimachos.interfaces import HttpClient
from tests.unit.fakes import FixtureHttp, UnrecordedRequest


def test_fixture_folder_replay() -> None:
    http = FixtureHttp("http")
    assert isinstance(http, HttpClient)
    resp = http.get(
        "https://api.example.org/works",
        params={"per-page": "2", "filter": "from_publication_date:2026-10-17"},
    )
    assert resp is not None
    assert resp.text().count("10.0000/example") == 2
    assert http.get("https://api.example.org/works/10.0000/missing") is None
    assert http.errors[-1].status == 404
    assert http.errors[-1].message == "api.example.org HTTP 404: Resource not found."
    assert len(http.requests) == 2 and http.unused() == []


def test_unrecorded_request_raises_and_is_logged() -> None:
    http = FixtureHttp()
    with pytest.raises(UnrecordedRequest):
        http.get("https://api.example.org/unknown")
    assert http.urls() == ["https://api.example.org/unknown"]
    lenient = FixtureHttp(strict=False)
    assert lenient.get("https://api.example.org/unknown") is None


def test_sequence_and_secret_params_ignored_for_matching() -> None:
    http = FixtureHttp()
    http.add("https://api.example.org/p", params={"cursor": "*"}, body={"page": 1})
    http.add("https://api.example.org/p", params={"cursor": "*"}, body={"page": 2})
    first = http.get("https://api.example.org/p", params={"cursor": "*", "api_key": "k"})
    second = http.get("https://api.example.org/p", params={"cursor": "*"})
    assert first is not None and second is not None
    assert b'"page": 1' in first.body and b'"page": 2' in second.body
    assert http.requests[0].params["api_key"] == "k"  # the log keeps what was sent
