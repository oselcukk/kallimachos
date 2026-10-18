"""``feedback add | parse | poll | apply-weights`` wiring (L4 libraries faked by protocol)."""

from __future__ import annotations

import io
import json
import shutil
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from kallimachos.cli import main
from kallimachos.config.schema import (
    Config,
    FeedbackConfig,
    MarkdownNotifyConfig,
    NotifyConfig,
    TelegramNotifyConfig,
    WeightLearningConfig,
)
from kallimachos.feedback import weights as fb_weights
from kallimachos.interfaces import (
    ExitCode,
    FeedbackChannel,
    FeedbackTag,
    MultWeight,
    RunKind,
    WeightChange,
    Weights,
)
from kallimachos.model.none import deterministic_selection
from kallimachos.notify import telegram
from tests.unit.test_run_support import FIXTURES, Env, install, make_run, validate

RID = "2026-10-18-daily"
TOKEN = "123456:EXAMPLEtokenEXAMPLEtoken"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Env:
    e = install(monkeypatch, tmp_path)
    run = make_run(6)
    e.save_run(run)
    sel = deterministic_selection(run, e.config.ranking.__class__(main_items=4, near_miss=2))
    # message order differs from run order: number 1 is candidate 3
    sel.message_no = {"1": run.candidates[2].id, "2": run.candidates[0].id}
    e.save_selection(sel)
    weekly = make_run(3, kind=RunKind.WEEKLY)
    e.save_run(weekly)
    return e


def test_add_resolves_through_selection_and_queues_read(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["feedback", "add", "--run", RID, "--item", "1", "--tag", "read"]) == 0
    entry = env.feedback[0]
    assert entry.candidate_id == "arXiv:2510.00003" and entry.resolved_via == "selection"
    assert entry.channel is FeedbackChannel.CLI and entry.tag is FeedbackTag.READ
    from kallimachos.interfaces import dump

    validate(dump(entry), "feedback.schema.json")
    assert env.queue == [
        {
            "ident": "arXiv:2510.00003",
            "kind": "read",
            "title": "Example title number 3",
            "url": "https://arxiv.org/abs/2510.00003",
            "note": None,
        }
    ]
    out = capsys.readouterr().out
    assert out.strip() == (
        "Item 1 (Example title number 3) recorded as 'good'".replace("good", "read")
        + " and added to the reading queue."
    )
    assert main(["feedback", "add", "--run", RID, "--item", "1", "--tag", "read"]) == 0
    assert len(env.feedback) == 1 and len(env.queue) == 1
    assert "already recorded" in capsys.readouterr().out


def test_add_archive_and_unknown_item(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["feedback", "add", "--run", RID, "--item", "6", "--tag", "archive"]) == 0
    assert env.feedback[0].resolved_via == "run_no" and env.queue == []
    assert "queued for archiving" in capsys.readouterr().out
    assert (
        main(["feedback", "add", "--run", RID, "--item", "99", "--tag", "good"]) == ExitCode.USAGE
    )


def test_parse_prints_resolution(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--json", "feedback", "parse", "1,2 good\n7 maybe", "--run", RID]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["resolved"] == {"1": "arXiv:2510.00003", "2": "arXiv:2510.00001"}
    assert data[1]["ambiguous"] == ["maybe"]
    assert main(["feedback", "parse", "3 iyi"]) == 0
    assert capsys.readouterr().out.strip() == "3: good"


def test_poll_markdown_is_idempotent(env: Env, tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    shutil.copy(FIXTURES / "notify" / "daily_ticked.md", daily / "2026-10-18.md")
    env.config = Config(
        notify=NotifyConfig(markdown=MarkdownNotifyConfig(path=str(daily))),
        feedback=FeedbackConfig(channels=["markdown"]),
    )
    assert main(["feedback", "poll"]) == 0
    got = [(e.item_no, e.candidate_id, e.tag.value, e.channel.value) for e in env.feedback]
    # marker ids win over numbers: the block says item 1 is candidate 1
    assert got == [
        ("1", "arXiv:2510.00001", "good", "markdown"),
        ("3", "arXiv:2510.00003", "read", "markdown"),
    ]
    cursor = json.loads((env.layout.state / "intake_markdown.json").read_text())
    assert len(cursor["seen"]) == 2
    assert main(["feedback", "poll", "--channel", "markdown"]) == 0
    assert len(env.feedback) == 2
    (env.layout.state / "intake_markdown.json").unlink()  # even without cursor: store dedupes
    assert main(["feedback", "poll", "--channel", "markdown"]) == 0
    assert len(env.feedback) == 2


class FakeResponse(io.BytesIO):
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def test_poll_telegram_resolves_runs_and_reports(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env.config = Config(
        notify=NotifyConfig(
            telegram=TelegramNotifyConfig(bot_token_ref="env:EXAMPLE_TG", chat_id=4242)
        ),
        feedback=FeedbackConfig(channels=["telegram", "imap"]),
    )
    env.secrets["env:EXAMPLE_TG"] = TOKEN
    body = (FIXTURES / "intake" / "telegram_updates.json").read_bytes()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: FakeResponse(body))
    telegram.remember_sent(env.layout.state, [101], RID)
    code = main(["--json", "feedback", "poll"])
    report = json.loads(capsys.readouterr().out)
    assert code == ExitCode.PARTIAL  # imap has no account configured
    assert report["imap"]["error"].startswith("ConfigError")
    tg = report["telegram"]
    assert tg["fetched"] == 2 and tg["recorded"] == 3
    items = [(e.run_id, e.item_no, e.candidate_id, e.tag.value) for e in env.feedback]
    assert items == [
        (RID, "3", "arXiv:2510.00003", "good"),
        (RID, "2", "arXiv:2510.00001", "irrelevant"),  # no reply: latest daily run by date
        (RID, "4", "arXiv:2510.00004", "irrelevant"),
    ]
    assert json.loads(env.layout.telegram_offset_file.read_text()) == {"offset": 505}


def test_poll_unresolvable_is_reported_not_recorded(env: Env) -> None:
    from kallimachos.cli_feedback import ChannelReport, process_raw
    from kallimachos.interfaces import RawFeedback

    report = ChannelReport()
    raw = RawFeedback(
        at=__import__("datetime").datetime(2026, 10, 1, tzinfo=__import__("datetime").UTC),
        channel=FeedbackChannel.TELEGRAM,
        text="H1 good\n2 good",
    )
    process_raw(raw, env.layout, env.config, report, dry_run=False)
    assert env.feedback == [] and len(report.unresolved) == 2


def test_apply_weights_propose_confirm_auto(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    proposal = fb_weights.WeightProposal("channel", "A", 1.0, 0.9, 0, 3)
    seen: dict[str, Any] = {}

    def propose(feedback: Any, runs: Any, weights: Weights, cfg: Any) -> list[Any]:
        seen["runs"] = sorted(runs)
        return [proposal]

    def apply(weights: Weights, proposals: Any, *, at: Any) -> Weights:
        return Weights(
            updated_at=at,
            channel={"A": MultWeight(0.9)},
            history=[WeightChange(at=at, change="channel A 1.0->0.9", evidence_counts={"neg": 3})],
        )

    monkeypatch.setattr(fb_weights, "propose", propose)
    monkeypatch.setattr(fb_weights, "apply", apply)
    monkeypatch.setattr(fb_weights, "render_report", lambda p, lang: "| channel A | 1.0 -> 0.9 |")
    main(["feedback", "add", "--run", RID, "--item", "2", "--tag", "irrelevant"])
    capsys.readouterr()

    assert main(["feedback", "apply-weights"]) == 0
    assert not env.layout.weights_file.exists()  # propose mode writes nothing
    out = capsys.readouterr().out
    assert "1.0 -> 0.9" in out and "--confirm" in out
    assert seen["runs"] == [RID]
    assert main(["feedback", "apply-weights", "--auto"]) == ExitCode.APPROVAL_REQUIRED
    assert main(["--dry-run", "feedback", "apply-weights", "--confirm"]) == 0
    assert not env.layout.weights_file.exists()
    assert main(["feedback", "apply-weights", "--confirm"]) == 0
    written = json.loads(env.layout.weights_file.read_text())
    validate(written, "weights.schema.json")
    assert written["channel"]["A"]["mult"] == 0.9
    env.layout.weights_file.unlink()
    env.config.feedback = FeedbackConfig(weights=WeightLearningConfig(mode="auto"))
    assert main(["feedback", "apply-weights", "--auto"]) == 0
    assert env.layout.weights_file.exists()
