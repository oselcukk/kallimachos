"""Shared helpers for the scoring tests: fictional profile, record factory, in-memory state store.

The scoring tests replace the module functions of ``state.store`` with a
minimal in-memory/on-disk fake that keeps the same contract (append line, read lines, atomic
write). Everything here is fictional.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from kallimachos.config.schema import RankingConfig, ScoringConfig
from kallimachos.interfaces import (
    Author,
    Candidate,
    Record,
    RuleContext,
    RunKind,
    Weights,
    WorkKind,
    build,
)
from kallimachos.profile.schema import Profile
from kallimachos.scoring.candidate import build_candidate
from kallimachos.scoring.topic import ChannelScores, score
from kallimachos.state import store

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "scoring"
TODAY = dt.date(2026, 10, 18)
NOW = dt.datetime(2026, 10, 18, 7, 30, tzinfo=dt.UTC)
RUN_ID = "2026-10-18-daily"


def load_profile(name: str = "profile-tidal-harmonics.yaml") -> Profile:
    data = yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))
    return build(Profile, data)


def make_record(
    title: str,
    abstract: str | None = None,
    *,
    authors: list[str] | None = None,
    categories: list[str] | None = None,
    primary_category: str | None = None,
    sources: set[str] | None = None,
    issn: list[str] | None = None,
    venue: str | None = None,
    arxiv_id: str | None = None,
    doi: str | None = None,
    msc: list[str] | None = None,
    referenced_works: list[str] | None = None,
    kind: WorkKind = WorkKind.UNKNOWN,
    published: dt.date | None = None,
) -> Record:
    return Record(
        title=title,
        abstract=abstract,
        authors=[Author(name=n) for n in (authors or ["Example Author A"])],
        categories=list(categories or []),
        primary_category=primary_category,
        sources=set(sources or {"test"}),
        issn=list(issn or []),
        venue=venue,
        arxiv_id=arxiv_id,
        doi=doi,
        msc=list(msc or []),
        referenced_works=list(referenced_works or []),
        kind=kind,
        published=published or TODAY,
    )


def scored(
    record: Record,
    profile: Profile,
    *,
    weights: Weights | None = None,
    cfg: ScoringConfig | None = None,
    run_kind: RunKind = RunKind.DAILY,
) -> ChannelScores:
    return score(record, profile, weights or Weights(), cfg or ScoringConfig(), run_kind=run_kind)


def candidate(
    record: Record,
    profile: Profile,
    *,
    cid: str,
    keys: list[str] | None = None,
    weights: Weights | None = None,
    cfg: ScoringConfig | None = None,
    no: int = 0,
) -> Candidate:
    scores = scored(record, profile, weights=weights, cfg=cfg)
    return build_candidate(record, scores, profile, candidate_id=cid, keys=keys or [cid], no=no)


def simple_candidate(
    cid: str,
    score_value: float,
    *,
    channels: list[str] | None = None,
    full_journal: bool = False,
    noise: bool = False,
    certain: bool = False,
    always_show: bool = False,
    date: dt.date = TODAY,
) -> Candidate:
    return Candidate(
        no=0,
        id=cid,
        title=f"Title {cid}",
        channels=channels or ["T"],
        score=score_value,
        threshold=4.0,
        full_journal=full_journal,
        noise=noise,
        certain_pass=certain,
        always_show_author=always_show,
        date=date,
        keys=[cid],
    )


def context(profile: Profile, **extra: Any) -> RuleContext:
    seen = extra.pop("seen", {})
    exempt = extra.pop("exempt", frozenset())
    prior = extra.pop("prior_user_drops", frozenset())
    venue_indicator = extra.pop("venue_indicator", {})
    return RuleContext(
        run_kind=RunKind.DAILY,
        run_date=TODAY,
        profile=profile,
        weights=Weights(),
        seen=seen,
        exempt=frozenset(exempt),
        prior_user_drops=frozenset(prior),
        venue_indicator=venue_indicator,
        extra=dict(extra),
    )


RANKING = RankingConfig()


# ---------------------------------------------------------------------------
# in-memory fake of state.store (store contract)
# ---------------------------------------------------------------------------


def _fake_append_jsonl(
    path: Path, record: Mapping[str, Any], *, schema: Mapping[str, Any] | None = None
) -> None:
    if schema is not None:
        import jsonschema

        jsonschema.validate(record, schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _fake_read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            obj = json.loads(line)
            assert isinstance(obj, dict)
            yield obj


def _fake_write_json_atomic(
    path: Path, data: Any, *, schema: Mapping[str, Any] | None = None
) -> None:
    if schema is not None:
        import jsonschema

        jsonschema.validate(data, schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _fake_read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _fake_write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "append_jsonl", _fake_append_jsonl)
    monkeypatch.setattr(store, "read_jsonl", _fake_read_jsonl)
    monkeypatch.setattr(store, "write_json_atomic", _fake_write_json_atomic)
    monkeypatch.setattr(store, "read_json", _fake_read_json)
    monkeypatch.setattr(store, "write_text_atomic", _fake_write_text_atomic)
