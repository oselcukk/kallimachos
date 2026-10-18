"""Representative instances of every state record validate against their schema."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from kallimachos.interfaces import (
    SCHEMA_REGISTRY,
    ArchiveLogLine,
    Candidate,
    Card,
    CardCitation,
    CardFlags,
    CardItem,
    CardReference,
    CatalogEntry,
    CatalogEvent,
    CatalogEventType,
    ClusterFrontmatter,
    ExistingEntry,
    FeedbackChannel,
    FeedbackEntry,
    FeedbackTag,
    GraphEdge,
    ItemType,
    Mismatch,
    MultWeight,
    NoteFrontmatter,
    ProcessedEntry,
    ReadingQueueEntry,
    RunKind,
    RunResult,
    SeenEntry,
    Selection,
    SemanticResult,
    StatusEntry,
    TextMeta,
    UnreachableEntry,
    Weights,
    Window,
    WorkKind,
    WorkState,
    build,
    dump,
)

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"
NOW = dt.datetime(2026, 10, 18, 7, 30, tzinfo=dt.UTC)
TODAY = NOW.date()
EID = "3f2a9c1b7d4e"

SAMPLES: dict[str, Any] = {
    "run.schema.json": RunResult(
        run_id="2026-10-18-daily",
        kind=RunKind.DAILY,
        date=TODAY,
        window=Window.ending_at(NOW, 4),
        candidates=[
            Candidate(
                no=1,
                id="arXiv:2510.00001",
                title="An illustrative result",
                authors=["Example Author A", "Example Author B"],
                authors_short="Example A, Example B",
                score=7.5,
                threshold=4.0,
                channels=["A"],
                channel_names=["Analytic number theory"],
                keys=["arxiv:2510.00001"],
                kind=WorkKind.PREPRINT,
            )
        ],
        dropped={"seen": 3},
    ),
    "selection.schema.json": Selection(
        run_id="2026-10-18-daily",
        written_at=NOW,
        by="rule",
        main=["arXiv:2510.00001"],
        message_no={"1": "arXiv:2510.00001"},
    ),
    "feedback.schema.json": FeedbackEntry(
        at=NOW,
        run_id="2026-10-18-daily",
        item_no="3",
        candidate_id="arXiv:2510.00001",
        tag=FeedbackTag.READ,
        channel=FeedbackChannel.TELEGRAM,
    ),
    "seen.schema.json": SeenEntry(
        keys=["arxiv:2510.00001"],
        first_run="2026-10-18-daily",
        last_run="2026-10-18-daily",
        shown=True,
        shown_run="2026-10-18-daily",
        shown_no=1,
        kind=WorkKind.PREPRINT,
        title_short="An illustrative",
    ),
    "weights.schema.json": Weights(updated_at=NOW, channel={"A": MultWeight(1.1)}),
    "catalog.schema.json": CatalogEntry(
        id=EID,
        keys=["arxiv:2510.00001"],
        arxiv="2510.00001",
        arxiv_version="v2",
        title="An illustrative result",
        authors=["Example Author A"],
        year=2026,
        kind=WorkKind.PREPRINT,
        channel="A",
        folder="A-Analytic-Number-Theory",
        path="A-Analytic-Number-Theory/Example_2026_Illustrative-result_arXiv-2510.00001v2.pdf",
        sha256="a" * 64,
        size=1234,
        pages=12,
        oa_source="arxiv",
        downloaded_at=NOW,
        triggers=["feedback:read"],
        events=[CatalogEvent(at=NOW, type=CatalogEventType.STORED)],
    ),
    "unreachable.schema.json": UnreachableEntry(
        id="doi:10.0000/example.1",
        title="Unreachable work",
        reason="403",
        oa_tried=["openalex"],
        folder="A-Example-Channel",
        filename="Author_2026_Unreachable-work.pdf",
    ),
    "processed.schema.json": ProcessedEntry(
        at=NOW, keys=["doi:10.0000/example.2"], status="filtered", reason="rule", trigger="feedback"
    ),
    "existing_catalog.schema.json": ExistingEntry(
        path="Folder/Example.pdf",
        root="/library",
        size=1024,
        mtime=1.0,
        sha256="a" * 64,
        doi="10.0000/example.3",
        kind_guess=WorkKind.ARTICLE,
        keys=["doi:10.0000/example.3"],
        identified_by=["filename"],
    ),
    "archive_log.schema.json": ArchiveLogLine(
        at=NOW, command="archive run", counts={"stored": 1}, errors=[]
    ),
    "text_meta.schema.json": TextMeta(
        eid=EID,
        path="text/3f2a9c1b7d4e.txt",
        pages=12,
        chars=40000,
        alpha_ratio=0.8,
        spaced_ratio=0.01,
    ),
    "card.schema.json": Card(
        eid=EID,
        path="A-Analytic-Number-Theory/Example.pdf",
        pages=12,
        citation=CardCitation(
            authors=["Example Author A"], year=2026, title="An illustrative result"
        ),
        summary="Fictional summary.",
        items=[
            CardItem(
                type=ItemType.THEOREM,
                label="Theorem 1.1",
                page=2,
                quote="x" * 60,
                statement="For every fictional parameter the statement holds.",
                number="1.1",
            )
        ],
        references=[
            CardReference(key="[1]", text="Example Author B, Another fictional paper, 2019.")
        ],
        flags=CardFlags(potential="low", rationale="synthetic"),
    ),
    "semantic_result.schema.json": SemanticResult(
        eid=EID,
        verifier_model="example-verifier",
        at=NOW,
        result="fail",
        mismatches=[Mismatch(kind="c", page=3, quote="q", note_sentence="s", severity="minor")],
    ),
    "note_frontmatter.schema.json": NoteFrontmatter(
        eid=EID,
        project="number-theory",
        channel="A",
        reader="example-reader",
        read_at=TODAY,
        updated_at=TODAY,
        source_pdf="A-Analytic-Number-Theory/Example.pdf",
        citekey="example2026illustrative",
    ),
    "cluster_frontmatter.schema.json": ClusterFrontmatter(
        cluster="A-Analytic-Number-Theory", project="number-theory", work_count=7, updated_at=TODAY
    ),
    "reading_queue.schema.json": ReadingQueueEntry(
        at=NOW,
        kind="read",
        title="An illustrative result",
        candidate_id="arXiv:2510.00001",
        url="https://arxiv.org/abs/2510.00001",
    ),
    "status.schema.json": StatusEntry(
        eid=EID, state=WorkState.CARD_WRITTEN, at=NOW, by="process card-write"
    ),
    "graph_edge.schema.json": GraphEdge(
        source_eid=EID, target_eid="0123456789ab", key="[1]", method="doi", score=100.0, text="ref"
    ),
}


@pytest.mark.parametrize(
    "name",
    sorted(k for k in SCHEMA_REGISTRY if k not in ("config.schema.json", "profile.schema.json")),
)
def test_sample_validates_and_round_trips(name: str) -> None:
    assert name in SAMPLES, f"add a sample for {name}"
    obj = SAMPLES[name]
    cls, _ = SCHEMA_REGISTRY[name]
    assert isinstance(obj, cls)
    schema = json.loads((SCHEMAS_DIR / name).read_text(encoding="utf-8"))
    data = dump(obj)
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        data
    )
    assert build(cls, data) == obj


def test_eid_pattern_enforced_by_schema() -> None:
    schema = json.loads((SCHEMAS_DIR / "status.schema.json").read_text(encoding="utf-8"))
    bad = dump(StatusEntry(eid="not-an-eid", state=WorkState.DOWNLOADED, at=NOW, by="x"))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(bad)


def test_card_quote_length_enforced_by_schema() -> None:
    schema = json.loads((SCHEMAS_DIR / "card.schema.json").read_text(encoding="utf-8"))
    card = SAMPLES["card.schema.json"]
    data = dump(card)
    data["items"][0]["quote"] = "too short"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(data)
