"""Integration (i): clean data directory -> init --answers -> profile approve -> run daily with the
real sources on recorded fixtures (model none) -> Markdown notification -> feedback by ticking
boxes -> apply-weights propose -> confirmed commit -> second run shows the weight in the score.

All real modules run; the only seam is the HTTP factory (recorded fixtures, no network).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kallimachos.interfaces import ExitCode
from kallimachos.paths import DataLayout
from tests.unit.test_integration_support import (
    ANSWERS,
    edit_config,
    jsonl,
    recorded_http,
    run_cli,
    source_fixtures,
)

RUN_DAY = "2026-10-13"  # the recorded fixtures carry works dated 2026-10-10 .. 2026-10-12
RID = f"{RUN_DAY}-daily"


def _tick_all(daily: Path, tag: str) -> int:
    """Tick every unticked item of the daily file and append ``#<tag>``; returns the count."""
    text = daily.read_text(encoding="utf-8")
    pattern = re.compile(r"^(- \[) (\] \d+\. .*)$", re.M)
    ticked, n = pattern.subn(lambda m: f"{m.group(1)}x{m.group(2)} #{tag}", text)
    daily.write_text(ticked, encoding="utf-8")
    return n


def test_init_run_feedback_weights_rerun(
    _isolated_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = DataLayout(_isolated_home)
    http = recorded_http(monkeypatch, *source_fixtures())

    # --- init from the fictional answers, approve the Q&A profile ------------------------------
    run_cli("init", "--answers", str(ANSWERS))
    assert layout.config_file.is_file() and layout.profile_file.is_file()
    assert run_cli("run", "daily", expect=None) == ExitCode.APPROVAL_REQUIRED  # draft profile
    run_cli("profile", "approve")
    run_cli("doctor")
    capsys.readouterr()

    # --- first daily run: arXiv + Crossref + OpenAlex fixtures, deterministic selection -------
    run_cli("run", "daily", "--date", RUN_DAY, "--first-run")
    out = capsys.readouterr().out
    assert out.startswith("Literature")
    run = json.loads(layout.run_file(RID).read_text(encoding="utf-8"))
    assert set(run["sources"]) == {"arxiv", "crossref", "openalex"}
    assert all(s["errors"] == [] for s in run["sources"].values()), run["sources"]
    assert run["pool_size"] >= 3  # the same works arrive from several sources and merge
    assert run["candidates"], run["dropped"]
    titles = [c["title"] for c in run["candidates"]]
    assert "Fictional bounds for twisted exponential sums" in titles  # core "exponential sums"
    exp = next(c for c in run["candidates"] if c["title"].startswith("Fictional bounds"))
    assert exp["channels"] == ["A"] and exp["score"] >= 6 and exp["doi"] == "10.0000/example.13"
    assert exp["rule_trace"] and exp["rule_trace"][-1]["rule"] == "certain_pass"
    assert {"arxiv", "crossref", "openalex"} <= set(exp["sources"])  # pool merge by DOI/title
    selection = json.loads(layout.selection_file(RID).read_text(encoding="utf-8"))
    assert selection["by"] == "rule" and selection["main"]
    seen = jsonl(layout.seen_file)
    assert seen and all(r["first_run"] == RID for r in seen)
    assert sum(1 for r in seen if r["shown"]) == len(selection["main"])
    # the OpenAlex ledger recorded the fixture's USD headers, not its credit count
    ledger = jsonl(layout.budget_dir / "openalex.jsonl")
    assert ledger and all(line["cost_usd"] == pytest.approx(0.0001) for line in ledger)
    assert all(line["remaining_usd"] == pytest.approx(0.0999) for line in ledger)
    assert all(line["remaining_credits"] == 999 for line in ledger)
    assert all(h.lower() != "authorization" for _m, _u, hdrs in http.calls for h in hdrs)
    assert all("User-Agent" in hdrs for _m, _u, hdrs in http.calls)

    # --- Markdown notification, feedback by ticking boxes --------------------------------------
    daily = layout.daily / f"{RUN_DAY}.md"
    text = daily.read_text(encoding="utf-8")
    assert f"<!-- kallimachos:begin daily run={RID} -->" in text
    assert f"<!-- kallimachos:item run={RID} id={exp['id']} -->" in text
    assert "[Open](" in text
    ticked = _tick_all(daily, "good")
    assert ticked == len(selection["main"])
    run_cli("--json", "feedback", "poll", "--channel", "markdown")
    report = json.loads(capsys.readouterr().out)["markdown"]
    assert report["recorded"] == ticked and report["unresolved"] == []
    entries = jsonl(layout.feedback_file)
    assert len(entries) == ticked and {e["tag"] for e in entries} == {"good"}
    assert {e["candidate_id"] for e in entries} >= {exp["id"]}
    run_cli("--json", "feedback", "poll", "--channel", "markdown")  # idempotent cursor
    assert json.loads(capsys.readouterr().out)["markdown"]["recorded"] == 0
    assert len(jsonl(layout.feedback_file)) == ticked

    # --- apply-weights: propose only, then the confirmed commit --------------------------------
    edit_config(layout, feedback__weights__min_evidence=1)
    run_cli("feedback", "apply-weights")
    proposal = capsys.readouterr().out
    assert "Proposed weight changes" in proposal and "| channel | A |" in proposal
    assert not layout.weights_file.exists()  # propose mode writes nothing
    run_cli("--json", "feedback", "apply-weights", "--confirm")
    committed = json.loads(capsys.readouterr().out)
    assert committed["written"] is True
    weights = json.loads(layout.weights_file.read_text(encoding="utf-8"))
    mult = weights["channel"]["A"]["mult"]
    assert mult > 1.0 and weights["history"]

    # --- second run: the learned channel multiplier changes the score --------------------------
    # --rerun forgets what the first attempt of this run recorded in seen, so the same recorded
    # works compete again and are scored under the new weights.
    run_cli("run", "daily", "--date", RUN_DAY, "--first-run", "--rerun", "--no-notify")
    capsys.readouterr()
    second = json.loads(layout.run_file(RID).read_text(encoding="utf-8"))
    exp2 = next(c for c in second["candidates"] if c["id"] == exp["id"])
    assert exp2["score"] > exp["score"]  # 13.0 -> channel and venue multipliers applied
    reasons = " ".join(exp2["score_reasons"])
    assert f"learned channel multiplier {mult:g}" in reasons
    assert "learned venue multiplier" in reasons  # the followed journal earned evidence too
    assert "learned" not in " ".join(exp["score_reasons"])


def test_offline_run_from_recorded_fixtures_without_any_patching(
    _isolated_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``OFFLINE=1`` + ``KALLIMACHOS_HTTP_FIXTURES`` produce a whole run with the real HTTP factory
    (no monkeypatched client): the way a user reproduces a run without a network."""
    import os

    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    roots = [fixtures / "arxiv", fixtures / "crossref", fixtures / "openalex"]
    monkeypatch.setenv("KALLIMACHOS_HTTP_FIXTURES", os.pathsep.join(str(r) for r in roots))
    monkeypatch.setenv("OFFLINE", "1")
    layout = DataLayout(_isolated_home)
    run_cli("init", "--answers", str(ANSWERS))
    run_cli("profile", "approve")
    code = run_cli("run", "daily", "--date", RUN_DAY, "--first-run", "--no-notify", expect=None)
    assert code in (int(ExitCode.OK), int(ExitCode.PARTIAL))
    run = json.loads(layout.run_file(RID).read_text(encoding="utf-8"))
    assert run["pool_size"] >= 1 and run["sources"]
    assert any(s["count"] for s in run["sources"].values())


def test_failed_run_leaves_a_failed_run_file_for_doctor(
    _isolated_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OFFLINE", "1")
    monkeypatch.delenv("KALLIMACHOS_HTTP_FIXTURES", raising=False)
    layout = DataLayout(_isolated_home)
    run_cli("init", "--answers", str(ANSWERS))
    run_cli("profile", "approve")
    capsys.readouterr()
    code = run_cli("run", "daily", "--date", RUN_DAY, "--no-notify", expect=None)
    assert code == int(ExitCode.EXHAUSTED)
    assert layout.run_file(RID).exists()
    run_cli("doctor", expect=None)
    out = capsys.readouterr().out
    assert "no runs yet" not in out and "failed" in out
