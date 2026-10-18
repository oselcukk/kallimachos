"""Archive identity parsing and triggers (manual.txt grammar)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from kallimachos.archive import triggers as trig
from kallimachos.archive.identity import key_of, normalize_key, parse_ident, record_keys
from kallimachos.interfaces import (
    ArchiveTrigger,
    Candidate,
    FeedbackEntry,
    FeedbackTag,
    Record,
    RunKind,
    RunResult,
)
from kallimachos.paths import DataLayout
from kallimachos.profile.schema import Channel, Profile


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("10.0000/Example.1", "doi:10.0000/example.1"),
        ("https://doi.org/10.0000/example.1.", "doi:10.0000/example.1"),
        ("doi:10.0000/example.1", "doi:10.0000/example.1"),
        ("10.48550/arXiv.2501.01234", "arxiv:2501.01234"),
        ("2501.01234v3", "arxiv:2501.01234"),
        ("arXiv:2501.01234", "arxiv:2501.01234"),
        ("https://arxiv.org/abs/2501.01234v2", "arxiv:2501.01234"),
        ("https://arxiv.org/pdf/math/0501234v1.pdf", "arxiv:math/0501234"),
        ("math.GT/0501234", "arxiv:math.GT/0501234"),
        ("2024/123", "eprint:2024/123"),
        ("https://eprint.iacr.org/2024/123.pdf", "eprint:2024/123"),
        ("W1234567", "oa:W1234567"),
        ("https://openalex.org/W1234567", "oa:W1234567"),
        ("ePrint:2024/123", "eprint:2024/123"),
        ("oa:W99", "oa:W99"),
    ],
)
def test_key_of(text: str, key: str) -> None:
    assert key_of(text) == key


def test_unknown_identifiers() -> None:
    assert parse_ident("hello world") is None
    assert parse_ident("") is None
    assert key_of("not-an-identifier") is None


def test_record_keys_versionless_and_no_arxiv_doi() -> None:
    rec = Record(
        title="t", doi="10.48550/arXiv.2501.01234", arxiv_id="2501.01234v2", openalex_id="W5"
    )
    assert record_keys(rec) == ["arxiv:2501.01234", "oa:W5"]
    assert normalize_key("arxiv:2501.01234v9") == "arxiv:2501.01234"


def test_parse_manual_grammar(fixtures_dir: Path) -> None:
    text = (fixtures_dir / "archive" / "manual.txt").read_text(encoding="utf-8")
    errors: list[str] = []
    out = trig.parse_manual(text, errors=errors)
    assert [t.ident for t in out] == [
        "doi:10.0000/example.1",
        "doi:10.0000/example.2",
        "arxiv:2501.01234",
        "eprint:2024/123",
        "oa:W1234567",
    ]
    assert out[0].channel_hint == "A"
    assert out[2].person_folder == "Example-Person-A"
    assert all(t.kind == "manual" for t in out)
    assert len(errors) == 2
    assert "line 7" in errors[0] and "not-an-identifier" in errors[0]
    assert "colour=red" in errors[1]


def test_parse_manual_rejects_unsafe_person_folder() -> None:
    errors: list[str] = []
    assert trig.parse_manual("10.0000/x person=../../etc", errors=errors) == []
    assert errors


def _run(kind: RunKind, cands: list[Candidate]) -> RunResult:
    day = dt.date(2026, 10, 18)
    return RunResult(
        run_id=f"{day.isoformat()}-{kind.value}", kind=kind, date=day, candidates=cands
    )


def test_collect_and_merge(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path)
    layout.archive.mkdir(parents=True)
    layout.manual_file.write_text("arXiv:2501.01234 channel=B\n", encoding="utf-8")
    at = dt.datetime(2026, 10, 18, 9, 0, tzinfo=dt.UTC)
    feedback = [
        FeedbackEntry(
            at=at, run_id="2026-10-18-daily", candidate_id="arXiv:2501.01234", tag=FeedbackTag.GOOD
        ),
        FeedbackEntry(
            at=at,
            run_id="2026-10-18-daily",
            candidate_id="doi:10.0000/example.9",
            tag=FeedbackTag.ARCHIVE,
        ),
        FeedbackEntry(
            at=at,
            run_id="2026-10-18-daily",
            candidate_id="doi:10.0000/example.8",
            tag=FeedbackTag.IRRELEVANT,
        ),
    ]
    runs = [
        _run(
            RunKind.DAILY,
            [
                Candidate(no=1, id="W77", title="t", certain_pass=True, channels=["A"]),
                Candidate(no=2, id="doi:10.0000/example.7", title="t", always_show_author=True),
            ],
        ),
        _run(
            RunKind.WEEKLY,
            [Candidate(no=1, id="doi:10.0000/example.6", title="t", always_show_author=True)],
        ),
    ]
    profile = Profile(channels=[Channel(id="A", name="Alpha", folder="Alpha")])
    errors: list[str] = []
    out = trig.collect_triggers(layout, feedback, runs, profile, errors=errors)
    by_key = {t.ident: t for t in out}
    assert set(by_key) == {
        "arxiv:2501.01234",
        "doi:10.0000/example.9",
        "oa:W77",
        "doi:10.0000/example.6",
    }
    merged = by_key["arxiv:2501.01234"]
    assert merged.kind == "manual" and merged.channel_hint == "B"
    assert trig.is_exempt(merged)
    assert trig.is_exempt(by_key["doi:10.0000/example.9"])  # feedback:archive
    assert by_key["oa:W77"].kind == "auto_pass" and by_key["oa:W77"].channel_hint == "A"
    assert not trig.is_exempt(by_key["oa:W77"])
    assert by_key["doi:10.0000/example.6"].kind == "always_show_new"
    assert errors == ["manual: unknown channel 'B' for arxiv:2501.01234"]


def test_merge_keeps_hints_from_weaker_trigger() -> None:
    a = ArchiveTrigger(kind="auto_pass", ident="doi:10.0000/x", channel_hint="A")
    b = ArchiveTrigger(kind="feedback", ident="doi:10.0000/x", note="good")
    out = trig.merge([a, b])
    assert len(out) == 1
    assert out[0].kind == "feedback" and out[0].channel_hint == "A"


def test_labels() -> None:
    assert (
        trig.label(ArchiveTrigger(kind="feedback", ident="k", run_id="r", note="ask"))
        == "feedback:ask:r"
    )
    assert trig.label(ArchiveTrigger(kind="manual", ident="k", note="manual:3")) == "manual:3"
