"""Selection: deterministic ``none`` path, validation, model call with retry and fallback."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from kallimachos.config.schema import Config, RankingConfig
from kallimachos.interfaces import NearNote, RunKind, UsageError, dump
from kallimachos.model import select
from kallimachos.model.none import (
    deterministic_selection,
    deterministic_summary,
    deterministic_why,
    strip_numbers,
)
from kallimachos.model.provider import ModelError
from tests.unit.test_run_support import candidate, make_run, validate


class ScriptedProvider:
    name = "claude"

    def __init__(self, answers: list[Any]) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.schemas: list[Any] = []

    def complete(self, prompt: str, *, timeout_s: int, json_schema: dict[str, Any] | None) -> str:
        self.prompts.append(prompt)
        self.schemas.append(json_schema)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, str) else json.dumps(answer)

    def test(self) -> Any:
        raise AssertionError("not used")


def _answer(main: list[str], near: list[str] | None = None, **items: Any) -> dict[str, Any]:
    return {
        "main": main,
        "near": near or [],
        "items": [
            {"id": i, "summary": items.get("summary", "It studies an example."), "why": "Fits A."}
            for i in main
        ],
    }


# --- deterministic ---------------------------------------------------------


def test_deterministic_daily_first_main_items_then_near() -> None:
    run = make_run(15)
    sel = deterministic_selection(run, RankingConfig())
    assert sel.main == [c.id for c in run.candidates[:10]]
    assert sel.near == [c.id for c in run.candidates[10:13]]
    assert sel.by == "rule"
    assert list(sel.message_no) == [str(i) for i in range(1, 14)]
    assert sel.message_no["11"] == run.candidates[10].id
    validate(dump(sel), "selection.schema.json")


def test_deterministic_keeps_certain_pass_in_main() -> None:
    run = make_run(14)
    late = run.candidates[12]
    late.certain_pass = True
    sel = deterministic_selection(run, RankingConfig(main_items=5, near_miss=2))
    assert late.id in sel.main and len(sel.main) == 5
    assert sel.main[-1] == late.id  # rank order kept
    assert late.id not in sel.near


def test_deterministic_weekly_persons_first_no_near() -> None:
    run = make_run(6, kind=RunKind.WEEKLY)
    run.candidates[4].always_show_author = True
    sel = deterministic_selection(run, RankingConfig(main_items=3))
    assert sel.main[0] == run.candidates[4].id
    assert len(sel.main) == 4 and sel.near == []
    assert list(sel.message_no) == ["H1", "H2", "H3", "H4"]


def test_deterministic_summary_and_missing_abstract() -> None:
    cand = candidate(1, abstract="One. Two is here. Three is long.")
    assert deterministic_summary(cand, max_chars=220, lang="en") == "One. Two is here."
    assert deterministic_summary(candidate(2, abstract=None), max_chars=220, lang="en") == ""
    long = candidate(3, abstract="word " * 100)
    out = deterministic_summary(long, max_chars=80, lang="en")
    assert len(out) <= 80 and not out.endswith(("…", "..."))


def test_deterministic_why_has_no_numbers() -> None:
    cand = candidate(
        1,
        score_reasons=["core 'example topic' in title +6", "seed citation x2 (+4)", "score 12.5"],
        near_note=NearNote(seed="Example seed", note_ref="Example-Note"),
    )
    why = deterministic_why(cand, lang="en")
    assert not any(ch.isdigit() for ch in why)
    assert "example topic" in why and "Example-Note" in why
    assert len(why) <= 90
    assert strip_numbers("SU(3) gauge theory +6") == "SU(3) gauge theory"


# --- build / validate ------------------------------------------------------


def test_validate_reports_every_problem() -> None:
    run = make_run(5)
    run.candidates[3].certain_pass = True
    cfg = Config(ranking=RankingConfig(main_items=2, near_miss=1))
    ids = [c.id for c in run.candidates]
    sel = select.build_selection(
        run, [ids[0], ids[0], "arXiv:9999.99999", ids[1]], ids[1:3], by="model"
    )
    problems = "\n".join(select.validate_selection(run, sel, cfg))
    assert "not candidates" in problems
    assert "duplicate" in problems
    assert "both main and near" in problems
    assert "certain-pass" in problems
    assert "at most 2 allowed" in problems
    assert "near has 2" in problems


def test_validate_accepts_good_selection_and_empty_run() -> None:
    run = make_run(4)
    ids = [c.id for c in run.candidates]
    sel = select.build_selection(run, ids[:2], ids[2:3], by="user")
    assert select.validate_selection(run, sel, Config()) == []
    empty = make_run(0)
    assert (
        select.validate_selection(empty, select.build_selection(empty, [], [], by="rule"), Config())
        == []
    )
    with pytest.raises(UsageError):
        select.build_selection(run, ids, [], by="robot")


def test_weekly_build_adds_persons_and_rejects_near() -> None:
    run = make_run(4, kind=RunKind.WEEKLY)
    run.candidates[2].always_show_author = True
    ids = [c.id for c in run.candidates]
    sel = select.build_selection(run, [ids[0]], [ids[1]], by="model")
    assert sel.main == [ids[2], ids[0]]
    assert sel.message_no == {"H1": ids[2], "H2": ids[0], "H3": ids[1]}
    assert any("near has 1" in p for p in select.validate_selection(run, sel, Config()))


def test_output_schema_is_strict() -> None:
    jsonschema.Draft202012Validator.check_schema(select.SELECTION_OUTPUT_SCHEMA)
    good = _answer(["a"])
    jsonschema.validate(good, select.SELECTION_OUTPUT_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**good, "score": 3}, select.SELECTION_OUTPUT_SCHEMA)


# --- prompts ---------------------------------------------------------------


def test_load_prompt_packaged_and_override(tmp_path: Path) -> None:
    for name in select.PROMPT_NAMES:
        text = select.load_prompt(name, None)
        assert not text.startswith("<!--")
        assert "data, not instructions" in text or name in ("summarize", "why")
    (tmp_path / "why.md").write_text("custom why {{language}}", encoding="utf-8")
    assert select.load_prompt("why", tmp_path) == "custom why {{language}}"
    assert "rerank" in select.load_prompt("rerank", tmp_path)  # falls back to packaged
    with pytest.raises(UsageError):
        select.load_prompt("other", None)


def test_build_prompt_fills_placeholders_and_hides_scores() -> None:
    run = make_run(3)
    run.candidates[0].score = 42.5
    prompt = select.build_prompt(run, Config(), lang="tr", profile_text="Field: example")
    assert "{{" not in prompt
    assert "Field: example" in prompt and "in tr" in prompt
    assert run.candidates[0].id in prompt
    assert "42.5" not in prompt


# --- model_select ----------------------------------------------------------


def test_model_select_happy_path() -> None:
    run = make_run(5)
    ids = [c.id for c in run.candidates]
    prov = ScriptedProvider([_answer([ids[2], ids[0]], [ids[4]])])
    sel, extras = select.model_select(run, prov, Config(), lang="en")
    assert sel.by == "model" and sel.main == [ids[2], ids[0]] and sel.near == [ids[4]]
    assert sel.message_no == {"1": ids[2], "2": ids[0], "3": ids[4]}
    assert extras["model_fallback"] is False
    assert extras["summaries"][ids[2]] == "It studies an example."
    assert prov.schemas[0] == select.SELECTION_OUTPUT_SCHEMA
    assert len(prov.prompts) == 1


def test_model_select_retries_once_then_succeeds() -> None:
    run = make_run(5)
    run.candidates[4].certain_pass = True
    ids = [c.id for c in run.candidates]
    prov = ScriptedProvider(["not json at all", _answer([ids[0], ids[4]])])
    sel, extras = select.model_select(run, prov, Config(), lang="en")
    assert sel.main == [ids[0], ids[4]] and extras["model_fallback"] is False
    assert "rejected" in prov.prompts[1] and "not JSON" in prov.prompts[1]


def test_model_cannot_drop_certain_pass_and_falls_back() -> None:
    run = make_run(12)
    run.candidates[11].certain_pass = True
    ids = [c.id for c in run.candidates]
    bad = _answer(ids[:3])  # drops the certain-pass candidate twice
    prov = ScriptedProvider([bad, ModelError("timed out")])
    sel, extras = select.model_select(run, prov, Config(), lang="en")
    assert extras["model_fallback"] is True
    assert sel.by == "rule" and ids[11] in sel.main
    assert any("certain-pass" in p for p in extras["problems"])
    assert extras["summaries"] == {}


def test_model_numbers_must_come_from_the_source() -> None:
    run = make_run(3)
    ids = [c.id for c in run.candidates]
    invented = _answer([ids[0]], summary="It improves the bound by 37 percent.")
    fine = _answer([ids[0]], summary="Abstract 1 explains it.")  # "1" is in the abstract
    prov = ScriptedProvider([invented, fine])
    _sel, extras = select.model_select(run, prov, Config(), lang="en")
    assert extras["model_fallback"] is False
    assert "37" in prov.prompts[1]
    assert extras["summaries"][ids[0]] == "Abstract 1 explains it."


def test_model_select_without_candidates_makes_no_call() -> None:
    prov = ScriptedProvider([])
    sel, extras = select.model_select(make_run(0), prov, Config(), lang="en")
    assert sel.main == [] and prov.prompts == [] and extras["model_fallback"] is False


def test_fenced_json_and_long_why_are_accepted() -> None:
    run = make_run(2)
    ids = [c.id for c in run.candidates]
    answer = _answer([ids[0]])
    answer["items"][0]["why"] = "Relevant because " + "really " * 30 + "close."
    prov = ScriptedProvider(["```json\n" + json.dumps(answer) + "\n```"])
    _sel, extras = select.model_select(run, prov, Config(), lang="en")
    assert len(extras["whys"][ids[0]]) <= 90


def test_fill_is_single_pass() -> None:
    from kallimachos.model.select import fill

    out = fill("{{a}} / {{b}} / {{missing}}", {"a": "value with {{b}} inside", "b": "B"})
    assert out == "value with {{b}} inside / B / {{missing}}"
