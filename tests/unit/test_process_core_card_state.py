"""process.card, process.semantic, process.status: validation, writing through state.store,
state machine transitions written only by commands, TR-v4 migration."""

from __future__ import annotations

import datetime as _dt
import json

import pytest

from kallimachos.interfaces import SchemaViolation, UsageError, WorkState, dump
from kallimachos.paths import DataLayout
from kallimachos.process import card as card_mod
from kallimachos.process import semantic as semantic_mod
from kallimachos.process import status as status_mod
from tests.unit.conftest_pdf import EID_A, QUOTE_OK, fake_store, layout_fixture, make_card  # noqa: F401

NOW = _dt.datetime(2026, 10, 18, 12, 0, tzinfo=_dt.UTC)


def _semantic(result: str = "pass", **kw: object) -> dict[str, object]:
    data: dict[str, object] = {
        "schema": "kallimachos.semantic_result/1",
        "eid": EID_A,
        "verifier_model": "example-verifier",
        "at": NOW.isoformat(),
        "result": result,
        "mismatches": [],
        "pages_read_from_image": [2],
    }
    data.update(kw)
    return data


# --- status ------------------------------------------------------------------------------------


def test_record_state_appends_and_validates_transitions(layout: DataLayout) -> None:
    assert status_mod.current_states(layout) == {}
    with pytest.raises(UsageError):  # card_written is not an entry state
        status_mod.record_state(layout, EID_A, WorkState.CARD_WRITTEN, by="test")
    status_mod.ensure_started(layout, EID_A, by="process extract", at=NOW)
    status_mod.record_state(layout, EID_A, WorkState.TEXT_EXTRACTED, by="process extract", at=NOW)
    with pytest.raises(UsageError):
        status_mod.record_state(layout, EID_A, WorkState.NOTE_WRITTEN, by="test")
    entry = status_mod.record_state(layout, EID_A, WorkState.CARD_WRITTEN, by="process card-write")
    again = status_mod.record_state(layout, EID_A, WorkState.CARD_WRITTEN, by="process card-write")
    assert again == entry  # idempotent re-delivery, no new line
    lines = layout.status_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[-1])["state"] == "card_written"
    assert status_mod.current_state(layout, EID_A) is WorkState.CARD_WRITTEN
    table = status_mod.status_table(layout)
    assert table == [
        {
            "eid": EID_A,
            "state": "card_written",
            "at": entry.at.isoformat(),
            "by": "process card-write",
        }
    ]


def test_summary_text_counts(layout: DataLayout) -> None:
    status_mod.ensure_started(layout, EID_A, by="t")
    status_mod.record_state(layout, EID_A, WorkState.TEXT_EXTRACTED, by="t")
    text = status_mod.summary_text(layout, lang="en")
    assert "Works tracked: 1" in text
    assert "- text_extracted: 1" in text
    assert text.splitlines()[-1].startswith(
        "Öğrenilen: "
    )  # the label beyin expects, in any language
    assert status_mod.summary_text(layout, lang="tr").splitlines()[-1].startswith("Öğrenilen: ")


# --- card --------------------------------------------------------------------------------------


def test_validate_card_rejects_dead_key_and_short_quote() -> None:
    data = dump(make_card())
    data["unknown"] = 1
    with pytest.raises(SchemaViolation, match="unknown"):
        card_mod.validate_card(data)
    data = dump(make_card())
    data["items"][0]["quote"] = "too short"
    with pytest.raises(SchemaViolation, match="quote"):
        card_mod.validate_card(data)
    data = dump(make_card())
    data["items"][0]["quoted_from_other"] = True
    data["items"][0]["type"] = "definition"
    with pytest.raises(SchemaViolation, match="supporting"):
        card_mod.validate_card(data)
    assert card_mod.validate_card(dump(make_card())).eid == EID_A


def test_write_card_records_state_and_round_trips(layout: DataLayout) -> None:
    status_mod.ensure_started(layout, EID_A, by="t")
    status_mod.record_state(layout, EID_A, WorkState.TEXT_EXTRACTED, by="t")
    path = card_mod.write_card(layout, make_card())
    assert path == layout.cards_dir / f"{EID_A}.json"
    assert status_mod.current_state(layout, EID_A) is WorkState.CARD_WRITTEN
    loaded = card_mod.load_card(layout, EID_A)
    assert loaded == make_card()
    assert card_mod.load_card(layout, "ffffffffffff") is None
    assert [c.eid for c in card_mod.load_all_cards(layout)] == [EID_A]


def test_write_card_refuses_without_text(layout: DataLayout) -> None:
    with pytest.raises(UsageError):
        card_mod.write_card(layout, make_card())
    assert not (layout.cards_dir / f"{EID_A}.json").is_file()  # refused transition writes nothing


def test_migrate_tr_v4_maps_keys_and_values() -> None:
    old = {
        "eid": EID_A,
        "yol": "A/x.pdf",
        "sayfa_sayisi": 2,
        "kunye": {
            "yazarlar": ["Example Author A"],
            "yil": 2019,
            "baslik": "T",
            "yayin": "V",
            "sayfa": 3,
        },
        "ozet_tr": "ozet",
        "ogeler": [
            {
                "tur": "open_problem",
                "numara": None,
                "baslik": "Problem",
                "sayfa": 1,
                "alinti": QUOTE_OK,
                "ifade_tr": "s",
                "kavramlar": [],
                "atif": [],
                "onem": "yardimci",
                "atif_edilen": False,
                "durum": "cozuldu_baska_eserde",
                "cozum_atif": ["[1]"],
                "somutluk": "ucu_acik",
            },
            {
                "tur": "claimed_solution",
                "numara": None,
                "baslik": "Claim",
                "sayfa": 1,
                "alinti": QUOTE_OK,
                "ifade_tr": "s",
                "onem": "ana",
                "atif_edilen": False,
                "cozum_turu": "karsi_ornek",
                "hedef": "Conjecture of [1]",
                "atif": ["[1]"],
            },
        ],
        "kaynakca": [{"anahtar": "[1]", "metin": "ref", "doi": None, "arxiv": None}],
        "kavramlar": ["lattice"],
        "isaretler": {
            "zorlandim": False,
            "potansiyel": "yuksek",
            "gerekce": "g",
            "ocr_zayif": False,
        },
        "yanlis_alarm": [{"sayfa": 1, "ifade": "Problem 9", "neden": "n"}],
    }
    new = card_mod.migrate_card_tr_v4(old)
    assert new["schema"] == "kallimachos.card/4"
    assert new["citation"] == {
        "authors": ["Example Author A"],
        "year": 2019,
        "title": "T",
        "venue": "V",
    }
    assert new["summary"] == "ozet"
    item = new["items"][0]
    assert item["type"] == "open_problem" and item["label"] == "Problem"
    assert item["importance"] == "supporting"
    assert item["status"] == "solved_elsewhere" and item["solution_cites"] == ["[1]"]
    assert item["concreteness"] == "open_ended"
    assert new["items"][1]["solution_kind"] == "counterexample" and new["items"][1]["target"]
    assert new["flags"]["potential"] == "high"
    assert new["false_alarms"] == [{"page": 1, "phrase": "Problem 9", "reason": "n"}]
    card = card_mod.validate_card(new)  # the migrated card is a valid card/4
    assert card.pages == 2 and card.path == "A/x.pdf"


# --- semantic ------------------------------------------------------------------------------------


def test_validate_semantic_rules() -> None:
    ok = semantic_mod.validate_semantic(_semantic(), eid=EID_A)
    assert ok.result == "pass" and ok.pages_read_from_image == [2]
    no_eid = _semantic()
    del no_eid["eid"]
    assert semantic_mod.validate_semantic(no_eid, eid=EID_A).eid == EID_A
    with pytest.raises(SchemaViolation, match="given"):
        semantic_mod.validate_semantic(_semantic(), eid="ffffffffffff")
    with pytest.raises(SchemaViolation, match="verifier_model"):
        semantic_mod.validate_semantic(_semantic(verifier_model=""), eid=EID_A)
    major = {"kind": "d", "page": 1, "quote": "q", "note_sentence": "n", "severity": "major"}
    with pytest.raises(SchemaViolation, match="major"):
        semantic_mod.validate_semantic(_semantic(mismatches=[major]), eid=EID_A)
    assert (
        semantic_mod.validate_semantic(_semantic("fail", mismatches=[major]), eid=EID_A).result
        == "fail"
    )
    with pytest.raises(SchemaViolation):
        semantic_mod.validate_semantic(_semantic(mismatches=[{**major, "kind": "e"}]), eid=EID_A)


def test_record_semantic_writes_state(layout: DataLayout) -> None:
    status_mod.ensure_started(layout, EID_A, by="t")
    for state in (WorkState.TEXT_EXTRACTED, WorkState.CARD_WRITTEN, WorkState.CHECK_PASSED):
        status_mod.record_state(layout, EID_A, state, by="t")
    fail = semantic_mod.validate_semantic(
        _semantic(
            "fail", mismatches=[{"kind": "a", "page": 1, "quote": "q", "note_sentence": "n"}]
        ),
        eid=EID_A,
    )
    semantic_mod.record_semantic(layout, fail)
    assert status_mod.current_state(layout, EID_A) is WorkState.SEMANTIC_FAILED
    ok = semantic_mod.validate_semantic(_semantic(), eid=EID_A)
    path = semantic_mod.record_semantic(layout, ok)
    assert path == layout.semantic_dir / f"{EID_A}.json"
    assert status_mod.current_state(layout, EID_A) is WorkState.SEMANTIC_VERIFIED
    latest = semantic_mod.latest_semantic(layout, EID_A)
    assert latest is not None and latest.result == "pass"
    assert semantic_mod.latest_semantic(layout, "ffffffffffff") is None
