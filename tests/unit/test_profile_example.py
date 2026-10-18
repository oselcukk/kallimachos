"""Profile: the shipped example profile validates; the offline fixture client behaves."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import yaml

from kallimachos.interfaces import build, dump
from kallimachos.profile.offline import FixtureHttp
from kallimachos.profile.schema import Profile

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "profiles" / "example-number-theory.yaml"
FIXTURES = ROOT / "tests" / "fixtures" / "profile" / "http"


def test_example_profile_validates_against_schema_and_builds() -> None:
    data = yaml.safe_load(EXAMPLE.read_text("utf-8"))
    schema = json.loads((ROOT / "schemas" / "profile.schema.json").read_text("utf-8"))
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        data
    )
    profile = build(Profile, data)
    assert profile.status == "draft" and not profile.is_approved
    assert [c.id for c in profile.channels] == ["A", "B", "C"]
    assert profile.channel_by_id("B") is not None and profile.channel_by_id("Z") is None
    assert build(Profile, dump(profile)) == profile


def test_fixture_http_replays_and_logs() -> None:
    http = FixtureHttp([FIXTURES])
    assert http.case_count > 10
    resp = http.get("https://api.openalex.org/works/doi:10.0000/example.1", kind="single")
    assert resp is not None and resp.status == 200 and resp.from_cache
    assert json.loads(resp.text())["id"].endswith("W1000000001")
    miss = http.get("https://api.openalex.org/works/doi:10.0000/none", kind="single")
    assert miss is None
    assert [e.message for e in http.errors] == ["no recorded fixture"]
    assert http.requests == [
        "https://api.openalex.org/works/doi:10.0000/example.1",
        "https://api.openalex.org/works/doi:10.0000/none",
    ]


def test_fixture_http_from_env_without_variable_is_empty(monkeypatch: object) -> None:
    http = FixtureHttp.from_env({})
    assert http.case_count == 0 and http.get("https://example.org/x") is None
