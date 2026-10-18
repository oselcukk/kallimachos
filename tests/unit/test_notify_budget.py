"""UTF-16 budget, shrink order and the ``count`` command."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from kallimachos.cli import main
from kallimachos.config.schema import Config, NotifyConfig
from kallimachos.interfaces import MessageBudget
from kallimachos.model.none import deterministic_selection
from kallimachos.notify import budget
from kallimachos.notify.budget import SUMMARY_SLOT, TITLE_SLOT, WHY_SLOT, count, fit, utf16_len
from kallimachos.notify.format import render
from tests.unit.test_run_support import candidate, install, make_run


def test_utf16_counts_surrogate_pairs() -> None:
    assert utf16_len("abc") == 3
    assert utf16_len("çğ") == 2  # BMP letters: one unit each
    assert utf16_len("\U0001d4d0") == 2  # mathematical script letter: surrogate pair
    assert utf16_len("\U0001f4da x") == 4


def test_count_reports_over() -> None:
    r = count("\U0001f4da" * 2000, limit=3900)
    assert (r.chars, r.utf16, r.limit, r.over) == (2000, 4000, 3900, True)
    assert not count("short").over


def _item(summary: str, why: str) -> dict[str, str]:
    return {
        "kind": "item",
        "template": f"\n\n1. Title\nSummary: {SUMMARY_SLOT}\nWhy: {WHY_SLOT}",
        "summary": summary,
        "why": why,
    }


def test_fit_shrinks_why_first_then_summary_then_near() -> None:
    why = "First why sentence. Second why sentence that is long."
    summary = "First summary sentence. " + "More summary words. " * 8
    near = {"kind": "near", "template": f"{TITLE_SLOT} (11)", "title": "Near title " * 10}
    parts = [_item(summary, why), near]
    total = utf16_len(budget.assemble(parts))
    why_cut = total - (len(why) - len("First why sentence."))
    out = fit(parts, MessageBudget(target_utf16=why_cut, limit_utf16=why_cut + 10))
    assert out[0]["why"] == "First why sentence."
    assert out[0]["summary"] == summary  # Why alone was enough
    assert out[1]["title"] == near["title"]
    out = fit(parts, MessageBudget(target_utf16=total - 150, limit_utf16=total))
    assert out[0]["summary"] == "First summary sentence."
    assert out[1]["title"] == near["title"]
    out = fit(parts, MessageBudget(target_utf16=40, limit_utf16=50))
    assert len(out) == 2 and len(out[1]["title"]) <= 30
    assert parts[0]["why"] == why  # input untouched
    for part in out:
        text = budget.render_part(part)
        assert "…" not in text and "..." not in text


def test_render_large_run_stays_within_target_and_keeps_items() -> None:
    run = make_run(13)
    for cand in run.candidates:
        cand.title = "A rather long example title about modular forms and lattices " * 2
        cand.abstract = ("This abstract sentence is fairly long and descriptive. " * 12).strip()
    cfg = Config()
    sel = deterministic_selection(run, cfg.ranking)
    msg = render(run, sel, cfg)
    assert utf16_len(msg.text) <= cfg.notify.budget.target_utf16
    for no in range(1, 11):
        assert f"\n{no}. " in msg.text
    assert "(11)" in msg.text and "(13)" in msg.text
    assert "…" not in msg.text and "..." not in msg.text


def test_count_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install(monkeypatch, tmp_path, Config(notify=NotifyConfig(budget=MessageBudget(3600, 10))))
    monkeypatch.setattr("sys.stdin", io.StringIO("çok \U0001f4da\n"))
    assert main(["count"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {"chars": 5, "utf16": 6, "limit": 10, "over": False}


def test_summary_budget_range() -> None:
    run = make_run(1)
    run.candidates[0] = candidate(1, abstract="Sentence. " * 60)
    msg = render(run, deterministic_selection(run, Config().ranking), Config())
    summary_line = next(line for line in msg.text.split("\n") if line.startswith("Summary: "))
    assert len(summary_line) - len("Summary: ") <= budget.SUMMARY_RANGE[1]
