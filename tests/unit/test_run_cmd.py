"""``run`` / ``select`` composition with fake sources and fake scoring."""

from __future__ import annotations

import datetime as _dt
import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from kallimachos import cli_run
from kallimachos.cli import build_parser, main
from kallimachos.config.schema import Config, MarkdownNotifyConfig, ModelConfig, NotifyConfig
from kallimachos.interfaces import (
    Candidate,
    ExitCode,
    FieldClass,
    HttpError,
    Record,
    RuleContext,
    RuleStage,
    RunKind,
    Verdict,
    VerdictKind,
    Window,
)
from kallimachos.profile.schema import Profile
from kallimachos.records import keys as record_keys
from kallimachos.records import pool as record_pool
from kallimachos.scoring import field as scoring_field
from kallimachos.scoring import seen as scoring_seen
from kallimachos.scoring import topic as scoring_topic
from kallimachos.scoring.rules import core as rules_core
from tests.unit.test_run_support import Env, approved_profile, install, record, validate

NOW = _dt.datetime(2026, 10, 18, 7, 30, tzinfo=_dt.UTC)


class FakeHttp:
    def __init__(self) -> None:
        self._errors: list[HttpError] = []

    def get(self, url: str, **kw: Any) -> Any:
        raise AssertionError("no HTTP in this test")

    @property
    def errors(self) -> list[HttpError]:
        return self._errors


class FakeSource:
    needs_key = False
    default_interval_s = 0.0

    def __init__(self, name: str, records: Sequence[Record] = (), fail: bool = False) -> None:
        self.name = name
        self.records = list(records)
        self.fail = fail
        self.windows: list[Window] = []

    def discover(self, window: Window, profile: Profile, http: Any) -> Iterator[Record]:
        self.windows.append(window)
        if self.fail:
            raise RuntimeError("HTTP 503 from example host")
        yield from self.records

    def metadata(self, ident: Any, http: Any) -> Record | None:
        return None

    def oa_pdf_candidates(self, record: Record, http: Any) -> list[Any]:
        return []

    def test(self, http: Any) -> Any:
        raise AssertionError


class FakeSeen:
    instances: list[FakeSeen] = []

    def __init__(self) -> None:
        self.recorded: list[tuple[str, bool, int | None]] = []
        self.saved = False
        self.blocked: set[str] = set()
        FakeSeen.instances.append(self)

    @classmethod
    def load(cls, path: Path) -> FakeSeen:
        return cls()

    def expire(self, *, today: _dt.date, grace_days: int) -> int:
        return 0

    def undo_run(self, run_id: str) -> int:
        self.undone = run_id
        return 0

    def is_blocked(self, record: Record, *, today: _dt.date, grace_days: int) -> bool:
        return "seen" in record.title

    def record_run(
        self, record: Record, keys: Sequence[str], run_id: str, *, shown: bool, shown_no: int | None
    ) -> None:
        self.recorded.append((record.title, shown, shown_no))

    def mark_shown(
        self, keys: Sequence[str], run_id: str, *, kind: Any, title: str, shown_no: int | None
    ) -> None:
        self.recorded.append((title, True, shown_no))

    def save(self) -> None:
        self.saved = True


def fake_score(record: Record, profile: Profile, weights: Any, cfg: Any, **kw: Any) -> Any:
    """Deterministic topic scores; the rule chain, ranking and quota are the real scoring code."""
    mentor = "mentor" in record.title
    ch = scoring_topic.ChannelScore(channel_id="A", final=8.0, threshold=4.0)
    passes = "low" not in record.title
    return scoring_topic.ChannelScores(
        by_channel={"A": ch},
        main_channel="A",
        channels=["A"],
        total=108.0 if mentor else 8.0,
        passes=passes,
        mandatory=mentor,
        always_show=mentor,
        reasons=["core 'example topic' in title +6"],
        bonuses={"always_show": 100.0} if mentor else {},
    )


class FakeCertainPass:
    """Stands in for the §6.2 certain-pass rule so the test controls which item is certain."""

    id = "certain_pass"
    stage = RuleStage.PASS

    def apply(self, cand: Candidate, ctx: RuleContext) -> Verdict:
        if "certain" in cand.title:
            cand.certain_pass = True
            return Verdict(VerdictKind.PASS, "certain")
        return Verdict.neutral()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Env:
    e = install(monkeypatch, tmp_path)
    e.config.notify.markdown.path = str(tmp_path / "daily")
    e.write_profile(approved_profile())

    def pool_add(self: record_pool.Pool, rec: Record) -> Record:
        self._records.append(rec)
        return rec

    monkeypatch.setattr(record_pool.Pool, "add", pool_add)
    monkeypatch.setattr(record_keys, "candidate_id", lambda r: f"arXiv:{r.arxiv_id}")
    monkeypatch.setattr(record_keys, "keys_for", lambda r: [f"arxiv:{r.arxiv_id}"])
    FakeSeen.instances = []
    monkeypatch.setattr(scoring_seen, "SeenIndex", FakeSeen)
    monkeypatch.setattr(scoring_topic, "score", fake_score)
    monkeypatch.setattr(scoring_field, "classify", lambda r: FieldClass.MATH)
    monkeypatch.setattr(rules_core, "CertainPassRule", FakeCertainPass)
    return e


def _records() -> list[Record]:
    return [
        record(1, title="Paper one about example topic"),
        record(2, title="Paper two certain pass"),
        record(3, title="Paper three low score"),
        record(4, title="Paper four predatory venue", venue="OMICS Journal of Examples"),
        record(5, title="Paper five already seen"),
        record(6, title="Paper six from the mentor"),
    ]


def _run(
    env: Env, *argv: str, sources: Sequence[FakeSource] | None = None, **kw: Any
) -> tuple[int, list[FakeSource]]:
    srcs = list(sources) if sources is not None else [FakeSource("arxiv", _records())]
    import argparse

    from kallimachos.cli import build_parser

    ns: argparse.Namespace = build_parser().parse_args(["run", *argv])
    code = cli_run.cmd_run(ns, sources=srcs, http=FakeHttp(), now=NOW, **kw)
    return code, srcs


def test_daily_run_writes_validated_files(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    code, srcs = _run(env, "daily")
    assert code == ExitCode.OK
    rid = "2026-10-18-daily"
    data = json.loads(env.layout.run_file(rid).read_text(encoding="utf-8"))
    validate(data, "run.schema.json")
    assert data["pool_size"] == 6 and data["passed"] == 3
    assert data["dropped"] == {"seen": 1, "threshold": 1, "predatory_venue": 1}
    assert data["dropped_examples"] == {"predatory_venue": ["arXiv:2510.00004"]}
    assert data["candidates"][0]["title"] == "Paper six from the mentor"
    assert data["first_run"] is False and data["model_fallback"] is False
    assert data["candidates"][0]["always_show_author"] is True
    sel = json.loads(env.layout.selection_file(rid).read_text(encoding="utf-8"))
    validate(sel, "selection.schema.json")
    assert sel["by"] == "rule" and len(sel["main"]) == 3
    assert env.layout.message_file(rid).read_text(encoding="utf-8").startswith("Literature")
    daily = Path(env.config.notify.markdown.path or "") / "2026-10-18.md"
    assert daily.read_text(encoding="utf-8").count("kallimachos:item") == 3
    assert srcs[0].windows[0].days == 1
    seen = FakeSeen.instances[0]
    assert seen.saved and all(shown for _t, shown, _n in seen.recorded)
    assert env.lock_events == ["acquire", "release"]
    assert "Literature" in capsys.readouterr().out


def test_first_run_uses_seven_day_window(env: Env) -> None:
    code, srcs = _run(env, "daily", "--first-run", "--no-notify")
    assert code == 0 and srcs[0].windows[0].days == 7
    assert srcs[0].windows[0].end == NOW
    assert json.loads(env.layout.run_file("2026-10-18-daily").read_text())["first_run"] is True
    assert not (Path(env.config.notify.markdown.path or "") / "2026-10-18.md").exists()


def test_same_day_rerun_needs_flag(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(env, "daily", "--no-notify")[0] == 0
    capsys.readouterr()
    code, srcs = _run(env, "daily")
    assert code == 0 and srcs[0].windows == []  # nothing recomputed
    captured = capsys.readouterr()
    assert captured.out.startswith("Literature") and "--rerun" in captured.err
    code, srcs = _run(env, "daily", "--rerun", "--no-notify")
    assert code == 0 and len(srcs[0].windows) == 1


def test_unapproved_profile_exits_5(env: Env) -> None:
    env.write_profile(Profile())
    with pytest.raises(Exception) as exc:
        _run(env, "daily")
    assert getattr(exc.value, "exit_code", None) == ExitCode.APPROVAL_REQUIRED


def test_cli_exit_codes_for_lock_and_unapproved(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from kallimachos.sources import registry

    monkeypatch.setattr(registry, "enabled_sources", lambda cfg: [FakeSource("arxiv", _records())])
    monkeypatch.setattr(cli_run.http_client, "from_config", lambda cfg, layout, **kw: FakeHttp())
    env.lock_held = True
    assert main(["run", "daily", "--date", "2026-10-18", "--no-notify"]) == ExitCode.LOCKED
    assert not env.layout.run_file("2026-10-18-daily").exists()
    env.lock_held = False
    env.write_profile(Profile())
    assert main(["run", "daily", "--date", "2026-10-18"]) == ExitCode.APPROVAL_REQUIRED


def test_all_sources_failed_exits_6_and_sends_failed_line(env: Env) -> None:
    sent: list[str] = []

    class Capture:
        name = "capture"

        def send(self, message: Any) -> Any:
            sent.append(message.text)
            from kallimachos.interfaces import NotifyResult

            return NotifyResult(channel="capture", ok=True)

        def test(self) -> Any:
            raise AssertionError

    with pytest.raises(Exception) as exc:
        _run(env, "daily", sources=[FakeSource("arxiv", fail=True)], notifiers=[Capture()])
    assert getattr(exc.value, "exit_code", None) == ExitCode.EXHAUSTED
    assert sent and sent[0].startswith("Literature scan did not run: arxiv: RuntimeError: HTTP 503")
    # the failure leaves a run file marked failed (doctor: "failed", not "no runs yet") ...
    from kallimachos.interfaces import RunResult, build
    from kallimachos.state.runs import run_failed

    def saved() -> RunResult:
        raw = env.layout.run_file("2026-10-18-daily").read_text(encoding="utf-8")
        return build(RunResult, json.loads(raw))

    failed = saved()
    assert run_failed(failed) and failed.candidates == [] and failed.errors
    # ... and it never blocks a retry on the same day (no --rerun needed)
    code, _ = _run(env, "daily", sources=[FakeSource("arxiv")], notifiers=[Capture()])
    assert code == ExitCode.OK and not run_failed(saved())


def test_one_source_failing_is_partial(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    code, _ = _run(
        env,
        "daily",
        sources=[FakeSource("arxiv", _records()), FakeSource("crossref", fail=True)],
    )
    assert code == ExitCode.PARTIAL
    data = json.loads(env.layout.run_file("2026-10-18-daily").read_text())
    assert data["sources"]["crossref"]["errors"] == [
        "crossref: RuntimeError: HTTP 503 from example host"
    ]
    assert data["sources"]["arxiv"]["count"] == 6
    out = capsys.readouterr()
    assert "Note: crossref did not respond" in out.out and "crossref" in out.err


def test_markdown_user_file_without_yes_exits_5_after_saving(env: Env) -> None:
    daily = Path(env.config.notify.markdown.path or "")
    daily.mkdir(parents=True)
    (daily / "2026-10-18.md").write_text("The user's own page.\n", encoding="utf-8")
    code, _ = _run(env, "daily")
    assert code == ExitCode.APPROVAL_REQUIRED
    assert (daily / "2026-10-18.md").read_text(encoding="utf-8") == "The user's own page.\n"
    assert env.layout.run_file("2026-10-18-daily").exists()


class BrokenModel:
    name = "claude"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, *, timeout_s: int, json_schema: Any) -> str:
        self.calls += 1
        return '{"main": [], "near": [], "items": []}'

    def test(self) -> Any:
        raise AssertionError


def test_model_fallback_is_recorded(env: Env) -> None:
    env.config.model = ModelConfig(provider="claude")
    model = BrokenModel()
    code, _ = _run(env, "daily", "--no-notify", provider=model)
    assert code == 0 and model.calls == 2
    data = json.loads(env.layout.run_file("2026-10-18-daily").read_text())
    assert data["model_fallback"] is True and data["selection_rule"] == "rule (model fallback)"
    sel = json.loads(env.layout.selection_file("2026-10-18-daily").read_text())
    assert "arXiv:2510.00002" in sel["main"]  # certain pass survives
    model.calls = 0
    _run(env, "daily", "--no-notify", "--no-model", "--rerun", provider=model)
    assert model.calls == 0


def test_dry_run_writes_nothing(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    import argparse

    from kallimachos.cli import build_parser

    ns: argparse.Namespace = build_parser().parse_args(["--dry-run", "--json", "run", "weekly"])
    code = cli_run.cmd_run(ns, sources=[FakeSource("arxiv", _records())], http=FakeHttp(), now=NOW)
    assert code == 0
    assert not env.layout.runs.exists() and env.lock_events == []
    assert not FakeSeen.instances[0].saved
    data = json.loads(capsys.readouterr().out)
    assert data["run_id"] == "2026-10-18-weekly" and data["kind"] == RunKind.WEEKLY.value


def test_select_command_validates_and_writes(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    _run(env, "daily", "--no-notify")
    capsys.readouterr()
    rid = "2026-10-18-daily"
    bad = main(["select", "--run", rid, "--main", "arXiv:2510.00001,arXiv:9999.00000"])
    assert bad == ExitCode.USAGE
    assert "not candidates" in capsys.readouterr().err
    missing = main(["select", "--run", rid, "--main", "arXiv:2510.00001"])
    assert missing == ExitCode.USAGE  # certain-pass arXiv:2510.00002 left out
    capsys.readouterr()
    ok = main(
        [
            "--json",
            "select",
            "--run",
            rid,
            "--main",
            "arXiv:2510.00002, arXiv:2510.00006",
            "--near",
            "arXiv:2510.00001",
        ]
    )
    assert ok == 0
    sel = json.loads(env.layout.selection_file(rid).read_text())
    assert sel["by"] == "model"
    assert sel["message_no"] == {
        "1": "arXiv:2510.00002",
        "2": "arXiv:2510.00006",
        "3": "arXiv:2510.00001",
    }
    assert json.loads(capsys.readouterr().out)["main"] == sel["main"]
    # the agent delivers this message: select marks the numbered candidates shown, under the lock
    marked = FakeSeen.instances[-1]
    assert marked.saved and [(s, n) for _t, s, n in marked.recorded] == [
        (True, 1),
        (True, 2),
        (True, 3),
    ]
    assert env.lock_events[-2:] == ["acquire", "release"]
    assert main(["select", "--run", "not-a-run", "--main", "x"]) == ExitCode.USAGE


def test_catch_up_window_days_widen_the_run(env: Env) -> None:
    """``schedule run-missed`` hands ``window_days`` to ``cmd_run``: one run covers every missed
    day; without it the daily window is one day."""
    ns = build_parser().parse_args(["run", "daily", "--date", "2026-10-18", "--no-notify"])
    ns.window_days = 3
    srcs = [FakeSource("arxiv", _records())]
    assert cli_run.cmd_run(ns, sources=srcs, http=FakeHttp(), now=NOW) == ExitCode.OK
    assert srcs[0].windows[0].days == 3
    data = json.loads(env.layout.run_file("2026-10-18-daily").read_text(encoding="utf-8"))
    assert data["first_run"] is False
    start = _dt.datetime.fromisoformat(data["window"]["start"])
    end = _dt.datetime.fromisoformat(data["window"]["end"])
    assert (end - start).days == 3
    assert cli_run.run_window(_dt.date(2026, 10, 18), NOW, RunKind.DAILY, False).days == 1
    assert (
        cli_run.run_window(_dt.date(2026, 10, 18), NOW, RunKind.DAILY, True, window_days=2).days
        == 2
    )


def test_window_for_past_date() -> None:
    w = cli_run.run_window(_dt.date(2026, 10, 10), NOW, RunKind.WEEKLY, False)
    assert w.end == _dt.datetime(2026, 10, 11, tzinfo=_dt.UTC) and w.days == 7


def test_profile_text_and_missing_profile(env: Env) -> None:
    text = cli_run.profile_text(approved_profile())
    assert "Channel A (Topic A" in text and "Field: Example field" in text
    env.layout.profile_file.unlink()
    with pytest.raises(Exception, match="kallimachos init"):
        cli_run.load_profile(env.layout)


def test_run_uses_the_shared_http_factory(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """``run weekly`` asks ``http.client.from_config`` for the weekly budget target; the client
    carries the OpenAlex ledger, the cache and the registry intervals."""
    from kallimachos.http import client

    http = client.from_config(Config(), env.layout, weekly=True)
    assert http.offline is True and "kallimachos" in http.ua.lower() and http.weekly
    assert http.budget is not None
    assert http.budget.ledger_path == env.layout.budget_dir / "openalex.jsonl"
    assert http.cache is not None and http.policy.interval_for("export.arxiv.org") >= 3.1
    assert http.policy.interval_for("api.openalex.org") >= 0.15


def test_markdown_config_default(env: Env) -> None:
    assert Config(notify=NotifyConfig(markdown=MarkdownNotifyConfig())).notify.markdown.path is None
