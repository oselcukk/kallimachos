"""Feedback store and the mandatory weight-learning tests."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from kallimachos.config.schema import ScoringConfig, WeightLearningConfig
from kallimachos.feedback import weights as fw
from kallimachos.feedback.store import (
    append_feedback,
    effective_tags,
    load_feedback,
    resolve_item,
)
from kallimachos.interfaces import (
    FeedbackChannel,
    FeedbackEntry,
    FeedbackTag,
    RunKind,
    RunResult,
    Selection,
    UsageError,
    Weights,
    build,
    dump,
)
from kallimachos.paths import DataLayout
from kallimachos.scoring.topic import score
from tests.unit.test_scoring_support import (  # noqa: F401
    NOW,
    RUN_ID,
    TODAY,
    candidate,
    fake_store,
    load_profile,
    make_record,
)

pytestmark = pytest.mark.usefixtures("fake_store")

CFG = WeightLearningConfig()


def _entry(cid: str, tag: FeedbackTag, *, run_id: str = RUN_ID, minute: int = 0) -> FeedbackEntry:
    return FeedbackEntry(
        at=NOW + dt.timedelta(minutes=minute),
        run_id=run_id,
        item_no="1",
        candidate_id=cid,
        tag=tag,
        channel=FeedbackChannel.CLI,
    )


def _run_with(cands):  # type: ignore[no-untyped-def]
    return RunResult(run_id=RUN_ID, kind=RunKind.DAILY, date=TODAY, candidates=list(cands))


def _t_run(n: int = 3) -> tuple[RunResult, list[str]]:
    """``n`` T-channel candidates whose score trace carries channel, phrases, venue and author."""
    profile = load_profile()
    cands = []
    ids = []
    for i in range(n):
        rec = make_record(
            f"Tidal harmonic study {i}",
            "nodal modulation and tide gauge records",
            issn=["1111-0002"],
            authors=["Example Verifiedsmith"],
        )
        cid = f"doi:10.0000/t.{i}"
        cands.append(candidate(rec, profile, cid=cid, no=i + 1))
        ids.append(cid)
    return _run_with(cands), ids


# ---------------------------------------------------------------- store


def test_append_feedback_dedupes_and_loads(tmp_path) -> None:  # type: ignore[no-untyped-def]
    layout = DataLayout(tmp_path)
    e = _entry("doi:10.0000/a", FeedbackTag.GOOD)
    assert append_feedback(layout, e) is True
    assert append_feedback(layout, e) is False  # same (run, candidate, tag)
    assert append_feedback(layout, _entry("doi:10.0000/a", FeedbackTag.READ)) is True
    entries = load_feedback(layout)
    assert [x.tag for x in entries] == [FeedbackTag.GOOD, FeedbackTag.READ]
    raw = layout.feedback_file.read_text(encoding="utf-8").splitlines()
    assert json.loads(raw[0])["candidate_id"] == "doi:10.0000/a"


def test_effective_tags_last_decision_wins() -> None:
    entries = [
        _entry("x", FeedbackTag.IRRELEVANT, minute=0),
        _entry("x", FeedbackTag.GOOD, minute=5),
        _entry("y", FeedbackTag.GOOD, minute=1),
        _entry("y", FeedbackTag.IRRELEVANT, minute=2),
    ]
    assert effective_tags(entries) == {"x": FeedbackTag.GOOD, "y": FeedbackTag.IRRELEVANT}


def test_resolve_item_selection_then_run_no() -> None:
    run, ids = _t_run(3)
    sel = Selection(run_id=RUN_ID, written_at=NOW, message_no={"1": ids[2], "H1": ids[0]})
    assert resolve_item(run, sel, "1") == (ids[2], "selection")
    assert resolve_item(run, sel, "h1") == (ids[0], "selection")
    assert resolve_item(run, None, "2") == (ids[1], "run_no")
    with pytest.raises(UsageError):
        resolve_item(run, sel, "9")
    with pytest.raises(UsageError):
        resolve_item(run, None, "H1")
    with pytest.raises(UsageError):
        resolve_item(run, None, "abc")


# ---------------------------------------------------------------- formula


def test_update_multiplier_formula_clamp_and_min_evidence() -> None:
    assert fw.update_multiplier(1.0, 2, 0, CFG) is None  # below min_evidence 3
    assert fw.update_multiplier(1.0, 3, 0, CFG) == pytest.approx(1.0 * (1 + 0.15 * 3 / 4))
    assert fw.update_multiplier(1.0, 0, 3, CFG) == pytest.approx(1.0 * (1 - 0.15 * 3 / 4))
    assert fw.update_multiplier(1.6, 30, 0, CFG) == CFG.clamp[1]  # clamp high
    assert fw.update_multiplier(0.5, 0, 30, CFG) == CFG.clamp[0]  # clamp low
    custom = WeightLearningConfig(learning_rate=0.5, min_evidence=1, clamp=(0.9, 1.1))
    assert fw.update_multiplier(1.0, 1, 0, custom) == pytest.approx(1.1)


# ---------------------------------------------------------------- mandatory §8.5 tests


def test_three_irrelevant_lower_channel_score_three_good_raise_it() -> None:
    profile = load_profile()
    cfg = ScoringConfig()
    run, ids = _t_run(3)
    probe = make_record("Tidal harmonic study", "nodal modulation")
    before = score(probe, profile, Weights(), cfg).topic_score

    neg = [_entry(cid, FeedbackTag.IRRELEVANT) for cid in ids]
    proposals = fw.propose(neg, {RUN_ID: run}, Weights(), CFG)
    channel = next(p for p in proposals if p.unit_kind == "channel" and p.unit_id == "T")
    assert channel.pos == 0 and channel.neg == 3 and channel.new < channel.old == 1.0
    lowered = fw.apply(Weights(), proposals, at=NOW)
    after_neg = score(probe, profile, lowered, cfg).topic_score
    assert after_neg < before

    pos = [_entry(cid, FeedbackTag.GOOD) for cid in ids]
    raised = fw.apply(Weights(), fw.propose(pos, {RUN_ID: run}, Weights(), CFG), at=NOW)
    after_pos = score(probe, profile, raised, cfg).topic_score
    assert after_pos > before
    assert raised.channel["T"].mult == pytest.approx(1 + 0.15 * 3 / 4)


def test_phrase_author_and_venue_units_learned_from_trace() -> None:
    run, ids = _t_run(3)
    proposals = fw.propose(
        [_entry(cid, FeedbackTag.READ) for cid in ids], {RUN_ID: run}, Weights(), CFG
    )
    kinds = {(p.unit_kind, p.unit_id) for p in proposals}
    assert ("phrase", "T/tidal harmonic") in kinds
    assert ("phrase", "T/nodal modulation") in kinds
    assert ("venue", "1111-0002") in kinds
    assert ("author", "example verifiedsmith") in kinds
    author = next(p for p in proposals if p.unit_kind == "author")
    assert author.old == 0 and author.new == pytest.approx(5 * (1 + 0.15 * 3 / 4) - 5)
    applied = fw.apply(Weights(), proposals, at=NOW)
    assert applied.author["example verifiedsmith"].bonus == pytest.approx(author.new)
    assert applied.phrase["T/tidal harmonic"].mult > 1


def test_below_min_evidence_no_change() -> None:
    run, ids = _t_run(2)
    proposals = fw.propose(
        [_entry(cid, FeedbackTag.IRRELEVANT) for cid in ids], {RUN_ID: run}, Weights(), CFG
    )
    assert proposals == []
    assert fw.apply(Weights(), proposals, at=NOW).history == []


def test_clamp_applies_through_propose() -> None:
    run, ids = _t_run(3)
    start = Weights(channel={"T": fw.MultWeight(1.58)})
    proposals = fw.propose(
        [_entry(cid, FeedbackTag.GOOD) for cid in ids], {RUN_ID: run}, start, CFG
    )
    channel = next(p for p in proposals if p.unit_kind == "channel")
    assert channel.new == CFG.clamp[1]


def test_last_decision_counts_once_per_candidate() -> None:
    run, ids = _t_run(3)
    feedback = [_entry(cid, FeedbackTag.IRRELEVANT, minute=0) for cid in ids] + [
        _entry(cid, FeedbackTag.GOOD, minute=1) for cid in ids
    ]
    proposals = fw.propose(feedback, {RUN_ID: run}, Weights(), CFG)
    channel = next(p for p in proposals if p.unit_kind == "channel")
    assert (channel.pos, channel.neg) == (3, 0)


def test_propose_mode_writes_no_file_auto_and_confirm_write(tmp_path) -> None:  # type: ignore[no-untyped-def]
    layout = DataLayout(tmp_path)
    run, ids = _t_run(3)
    proposals = fw.propose(
        [_entry(cid, FeedbackTag.GOOD) for cid in ids], {RUN_ID: run}, Weights(), CFG
    )
    assert proposals
    assert fw.commit(layout, Weights(), proposals, CFG, at=NOW) is None
    assert not layout.weights_file.exists()  # propose mode: report only
    report = fw.render_report(proposals, lang="en")
    assert "| channel | T |" in report and "--confirm" in report
    assert fw.render_report([], lang="tr").strip().endswith("yeterli kanıt birikmedi.")

    confirmed = fw.commit(layout, Weights(), proposals, CFG, at=NOW, confirm=True)
    assert confirmed is not None and layout.weights_file.exists()
    on_disk = build(Weights, json.loads(layout.weights_file.read_text(encoding="utf-8")))
    assert on_disk.channel["T"].mult == pytest.approx(confirmed.channel["T"].mult)

    layout2 = DataLayout(tmp_path / "auto")
    auto = WeightLearningConfig(mode="auto")
    assert fw.commit(layout2, Weights(), proposals, auto, at=NOW) is not None
    assert layout2.weights_file.exists()
    assert fw.commit(layout2, Weights(), [], auto, at=NOW) is None


def test_history_entries_carry_evidence_counts() -> None:
    run, ids = _t_run(3)
    proposals = fw.propose(
        [_entry(cid, FeedbackTag.ARCHIVE) for cid in ids], {RUN_ID: run}, Weights(), CFG
    )
    applied = fw.apply(Weights(), proposals, at=NOW)
    assert applied.updated_at == NOW
    assert len(applied.history) == len(proposals) > 0
    for change in applied.history:
        assert change.evidence_counts == {"pos": 3, "neg": 0, "evidence": 3}
        assert " -> " in change.change
    dumped = dump(applied)  # serialisable for weights.schema.json
    assert dumped["schema"] == "kallimachos.weights/1"
    assert build(Weights, dumped).history[0].change == applied.history[0].change


def test_feedback_for_unknown_run_or_candidate_is_ignored() -> None:
    run, _ = _t_run(1)
    proposals = fw.propose(
        [_entry("doi:10.0000/none", FeedbackTag.GOOD)] * 3
        + [_entry("x", FeedbackTag.GOOD, run_id="2026-10-01-daily")] * 3,
        {RUN_ID: run},
        Weights(),
        CFG,
    )
    assert proposals == []
