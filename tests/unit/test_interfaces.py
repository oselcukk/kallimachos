"""Shared types: strict builder, dumper, Window, Ident, exit codes."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Literal

import pytest

from kallimachos.config.schema import Config, EmailNotifyConfig
from kallimachos.interfaces import (
    ApprovalRequired,
    BudgetExhausted,
    Candidate,
    ConfigError,
    ExitCode,
    FieldClass,
    Ident,
    IdentKind,
    KallimachosError,
    LockHeld,
    PartialFailure,
    RunKind,
    RunResult,
    SchemaViolation,
    UsageError,
    Window,
    build,
    dump,
    json_schema,
)


@dataclass
class Inner:
    n: int
    tag: Literal["a", "b"] = "a"


@dataclass
class Outer:
    name: str
    when: dt.date
    inner: Inner = field(default_factory=lambda: Inner(0))
    items: list[Inner] = field(default_factory=list)
    ratio: float = 1.0
    maybe: str | None = None
    kind: FieldClass = FieldClass.UNKNOWN
    by_prio: dict[int, float] = field(default_factory=dict)
    tags: set[str] = field(default_factory=set)


class TestBuild:
    def test_round_trip(self) -> None:
        data = {
            "name": "x",
            "when": "2026-10-18",
            "inner": {"n": 2, "tag": "b"},
            "items": [{"n": 1}],
            "ratio": 2,
            "kind": "math",
            "by_prio": {"1": 1.0, 2: 0.5},
            "tags": ["b", "a"],
        }
        obj = build(Outer, data)
        assert obj.when == dt.date(2026, 10, 18)
        assert obj.inner.tag == "b"
        assert obj.ratio == 2.0 and isinstance(obj.ratio, float)
        assert obj.kind is FieldClass.MATH
        assert obj.by_prio == {1: 1.0, 2: 0.5}
        dumped = dump(obj)
        assert dumped["when"] == "2026-10-18"
        assert dumped["kind"] == "math"
        assert dumped["by_prio"] == {"1": 1.0, "2": 0.5}
        assert dumped["tags"] == ["a", "b"]
        assert build(Outer, dumped) == obj

    def test_dead_key_is_an_error(self) -> None:
        with pytest.raises(SchemaViolation, match=r"unknown key.*bogus"):
            build(Outer, {"name": "x", "when": "2026-01-01", "bogus": 1})

    def test_nested_dead_key_reports_path(self) -> None:
        with pytest.raises(SchemaViolation) as exc:
            build(Outer, {"name": "x", "when": "2026-01-01", "inner": {"n": 1, "zz": 2}})
        assert exc.value.path == "inner"

    def test_missing_required(self) -> None:
        with pytest.raises(SchemaViolation, match="missing required key"):
            build(Outer, {"name": "x"})

    def test_bool_is_not_int(self) -> None:
        with pytest.raises(SchemaViolation, match="expected int"):
            build(Inner, {"n": True})

    def test_literal_and_enum_errors(self) -> None:
        with pytest.raises(SchemaViolation, match="expected one of"):
            build(Inner, {"n": 1, "tag": "z"})
        with pytest.raises(SchemaViolation, match="expected one of"):
            build(Outer, {"name": "x", "when": "2026-01-01", "kind": "biology"})

    def test_null_only_where_optional(self) -> None:
        obj = build(Outer, {"name": "x", "when": "2026-01-01", "maybe": None})
        assert obj.maybe is None
        with pytest.raises(SchemaViolation, match="null is not allowed"):
            build(Outer, {"name": None, "when": "2026-01-01"})

    def test_bad_date(self) -> None:
        with pytest.raises(SchemaViolation, match="ISO date"):
            build(Outer, {"name": "x", "when": "18/10/2026"})

    def test_alias_key(self) -> None:
        cfg = build(EmailNotifyConfig, {"from": "a@example.org"})
        assert cfg.from_ == "a@example.org"
        assert dump(cfg)["from"] == "a@example.org"
        with pytest.raises(SchemaViolation):
            build(EmailNotifyConfig, {"from_": "a@example.org"})

    def test_config_defaults_build_from_empty_mapping(self) -> None:
        cfg = build(Config, {})
        assert cfg.sources.arxiv.min_interval_s == 3.1
        assert cfg.privacy.send_only_strong_ids is True
        with pytest.raises(SchemaViolation):
            build(Config, {"privacy": {"send_only_strong_ids": False}})
        with pytest.raises(SchemaViolation):
            build(Config, {"privacy": {"personal_data_in_logs": True}})


class TestJsonSchema:
    def test_required_defaults_and_null(self) -> None:
        schema = json_schema(Outer, schema_id="urn:test")
        assert schema["$id"] == "urn:test"
        assert schema["required"] == ["name", "when"]
        assert schema["additionalProperties"] is False
        props = schema["properties"]
        assert props["when"] == {"type": "string", "format": "date"}
        assert props["maybe"] == {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
        assert props["inner"] == {"$ref": "#/$defs/Inner"}  # default_factory values are not emitted
        assert props["kind"]["enum"] == [m.value for m in FieldClass]
        assert props["by_prio"]["propertyNames"] == {"pattern": "^-?[0-9]+$"}
        assert props["tags"]["uniqueItems"] is True
        assert schema["$defs"]["Inner"]["properties"]["tag"] == {"enum": ["a", "b"], "default": "a"}


class TestWindow:
    def test_requires_tz_aware(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            Window(dt.datetime(2026, 1, 1), dt.datetime(2026, 1, 2, tzinfo=dt.UTC))

    def test_order(self) -> None:
        end = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        with pytest.raises(ValueError, match="before"):
            Window(end, end - dt.timedelta(days=1))

    def test_ending_at_and_contains(self) -> None:
        end = dt.datetime(2026, 10, 18, 7, 30, tzinfo=dt.UTC)
        w = Window.ending_at(end, 7)
        assert w.days == 7
        assert w.contains(end - dt.timedelta(hours=1))
        assert not w.contains(end)
        assert build(Window, dump(w)) == w


class TestIdent:
    @pytest.mark.parametrize(
        ("kind", "value", "key", "cid"),
        [
            (IdentKind.DOI, "10.1000/x.1", "doi:10.1000/x.1", "doi:10.1000/x.1"),
            (IdentKind.ARXIV, "2510.01234", "arxiv:2510.01234", "arXiv:2510.01234"),
            (IdentKind.EPRINT, "2026/001", "eprint:2026/001", "ePrint:2026/001"),
            (IdentKind.OPENALEX, "W123", "oa:W123", "W123"),
            (IdentKind.ZBMATH, "7000001", "zb:7000001", "zbMATH:7000001"),
            (IdentKind.PUBMED, "123456", "pmid:123456", "pmid:123456"),
            (IdentKind.RSS, "0123abcd", "rss:0123abcd", "rss:0123abcd"),
            (
                IdentKind.TITLE,
                "some long title key",
                "nt:some long title key",
                "nt:some long title key",
            ),
        ],
    )
    def test_key_and_candidate_id_round_trip(
        self, kind: IdentKind, value: str, key: str, cid: str
    ) -> None:
        ident = Ident(kind, value)
        assert ident.key == key
        assert ident.candidate_id == cid
        assert Ident.parse_key(key) == ident
        assert Ident.parse_candidate_id(cid) == ident

    def test_openalex_without_w_prefix(self) -> None:
        assert Ident(IdentKind.OPENALEX, "123").candidate_id == "W123"

    def test_invalid(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            Ident(IdentKind.DOI, "")
        with pytest.raises(ValueError, match="unknown key prefix"):
            Ident.parse_key("isbn:1")
        with pytest.raises(ValueError, match="not a candidate id"):
            Ident.parse_candidate_id("plain")


class TestExitCodes:
    def test_values_follow_spec(self) -> None:
        assert [int(c) for c in ExitCode] == [0, 1, 2, 3, 4, 5, 6]

    @pytest.mark.parametrize(
        ("exc", "code"),
        [
            (KallimachosError, ExitCode.UNEXPECTED),
            (UsageError, ExitCode.USAGE),
            (ConfigError, ExitCode.USAGE),
            (SchemaViolation, ExitCode.USAGE),
            (PartialFailure, ExitCode.PARTIAL),
            (LockHeld, ExitCode.LOCKED),
            (ApprovalRequired, ExitCode.APPROVAL_REQUIRED),
            (BudgetExhausted, ExitCode.EXHAUSTED),
        ],
    )
    def test_exception_exit_codes(self, exc: type[KallimachosError], code: ExitCode) -> None:
        assert exc("x").exit_code == code


def test_run_result_round_trip() -> None:
    end = dt.datetime(2026, 10, 18, tzinfo=dt.UTC)
    run = RunResult(
        run_id="2026-10-18-daily",
        kind=RunKind.DAILY,
        date=dt.date(2026, 10, 18),
        window=Window.ending_at(end, 4),
        candidates=[Candidate(no=1, id="arXiv:2510.00001", title="T", score=7.5, threshold=4)],
    )
    data = dump(run)
    assert data["schema"] == "kallimachos.run/1"
    assert data["candidates"][0]["field_class"] == "unknown"
    assert build(RunResult, data) == run
