"""Feedback intakes: Telegram getUpdates, IMAP replies, Markdown ticked boxes (fixtures only)."""

from __future__ import annotations

import imaplib
import io
import json
import shutil
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from kallimachos.config.schema import (
    Config,
    EmailNotifyConfig,
    FeedbackConfig,
    MarkdownNotifyConfig,
    NotifyConfig,
    ScholarImapConfig,
    SourcesConfig,
    TelegramNotifyConfig,
)
from kallimachos.feedback.intake_imap import ImapIntake, command_lines, imap_date
from kallimachos.feedback.intake_markdown import MarkdownIntake, split_ref
from kallimachos.feedback.intake_telegram import TelegramIntake
from kallimachos.interfaces import ConfigError, FeedbackChannel, FeedbackIntake
from kallimachos.notify import telegram
from tests.unit.test_run_support import FIXTURES, Env, install

INTAKE = FIXTURES / "intake"
NOTIFY = FIXTURES / "notify"
TOKEN = "123456:EXAMPLEtokenEXAMPLEtoken"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Env:
    return install(monkeypatch, tmp_path)


# --- Telegram --------------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _tg_config() -> Config:
    return Config(
        notify=NotifyConfig(
            telegram=TelegramNotifyConfig(bot_token_ref="env:EXAMPLE_TG", chat_id="4242")
        ),
        feedback=FeedbackConfig(channels=["telegram"]),
    )


def test_telegram_offset_chat_filter_and_reply_resolution(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.secrets["env:EXAMPLE_TG"] = TOKEN
    body = (INTAKE / "telegram_updates.json").read_bytes()
    seen: list[dict[str, Any]] = []

    def urlopen(req: urllib.request.Request, timeout: float = 0) -> FakeResponse:
        assert isinstance(req.data, bytes)
        seen.append(json.loads(req.data))
        return FakeResponse(body)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    telegram.remember_sent(env.layout.state, [101], "2026-10-18-daily")
    intake = TelegramIntake(_tg_config(), env.layout)
    assert isinstance(intake, FeedbackIntake)
    raws, cursor = intake.poll({})
    assert seen[0] == {"timeout": 0, "allowed_updates": ["message"]}
    assert [r.text for r in raws] == ["3 good", "2,4 alakasız"]
    assert raws[0].run_hint == "2026-10-18-daily" and raws[1].run_hint is None
    assert raws[0].channel is FeedbackChannel.TELEGRAM and raws[0].message_ref == "500"
    assert raws[0].at.tzinfo is not None
    assert cursor == {"offset": 505}
    intake.poll(cursor)
    assert seen[1]["offset"] == 505


# --- IMAP ------------------------------------------------------------------


class FakeIMAP:
    mailbox: dict[int, bytes] = {}
    instances: list[FakeIMAP] = []
    validity = 77

    def __init__(self, host: str, port: int, ssl_context: Any = None, timeout: float = 0) -> None:
        self.host, self.port = host, port
        self.log: list[Any] = []
        FakeIMAP.instances.append(self)

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.log.append(("login", user, password))
        return "OK", [b""]

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.log.append(("select", folder, readonly))
        return "OK", [str(len(self.mailbox)).encode()]

    def response(self, code: str) -> tuple[str, list[bytes]]:
        return code, [str(self.validity).encode()]

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        self.log.append(("uid", command, *args))
        if command == "SEARCH":
            return "OK", [b" ".join(str(u).encode() for u in sorted(self.mailbox))]
        uid = int(args[0])
        return "OK", [(f"{uid} (BODY[] {{1}}".encode(), self.mailbox[uid]), b")"]

    def logout(self) -> tuple[str, list[bytes]]:
        self.log.append(("logout",))
        return "BYE", [b""]


def _imap_config(**email_kw: Any) -> Config:
    return Config(
        notify=NotifyConfig(
            email=EmailNotifyConfig(
                smtp_host="smtp.example.org",
                username="reader@example.org",
                password_ref="keyring:kallimachos/smtp",
                from_="reader@example.org",
                to=["reader@example.org", "colleague@example.org"],
                **email_kw,
            )
        )
    )


@pytest.fixture
def imap(monkeypatch: pytest.MonkeyPatch) -> type[FakeIMAP]:
    FakeIMAP.instances = []
    FakeIMAP.mailbox = {
        uid: (INTAKE / name).read_bytes()
        for uid, name in enumerate(
            ["reply_daily.eml", "reply_subject_only.eml", "stranger.eml", "unrelated.eml"], 1
        )
    }
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    return FakeIMAP


def test_imap_resolves_run_and_filters_senders(env: Env, imap: type[FakeIMAP]) -> None:
    env.secrets["keyring:kallimachos/smtp"] = "pw"
    intake = ImapIntake(_imap_config(), env.layout)
    raws, cursor = intake.poll({})
    conn = imap.instances[0]
    assert (conn.host, conn.port) == ("imap.example.org", 993)
    assert ("select", "INBOX", True) in conn.log  # read-only
    assert conn.log[-1] == ("logout",)
    assert conn.log[2][:3] == ("uid", "SEARCH", "SINCE")
    assert [(r.run_hint, r.text) for r in raws] == [
        ("2026-10-18-daily", "3 good\n5 irrelevant: off topic"),
        ("2026-10-18-weekly", "H2 read"),
    ]
    assert (
        raws[0].message_ref == "<reply-1@example.org>" and raws[0].channel is FeedbackChannel.IMAP
    )
    assert cursor == {"uidvalidity": 77, "last_uid": 4}

    raws2, cursor2 = intake.poll(cursor)
    assert raws2 == [] and cursor2 == cursor
    assert imap.instances[1].log[2] == ("uid", "SEARCH", "UID 5:*")
    imap.validity = 78  # mailbox rebuilt: start over
    raws3, _ = intake.poll(cursor)
    assert len(raws3) == 2
    imap.validity = 77


def test_imap_account_resolution(env: Env) -> None:
    scholar = SourcesConfig(
        scholar_imap=ScholarImapConfig(
            host="mail.example.org", user_ref="env:EX_USER", password_ref="env:EX_PW", port=1993
        )
    )
    env.secrets["env:EX_USER"] = "alerts@example.org"
    cfg = _imap_config()
    cfg.sources = scholar
    assert ImapIntake(cfg, env.layout).settings() == (
        "mail.example.org",
        1993,
        "INBOX",
        "alerts@example.org",
        "env:EX_PW",
    )
    explicit = ImapIntake(_imap_config(), env.layout, host="imap.example.net", folder="Replies")
    assert explicit.settings()[:3] == ("imap.example.net", 993, "Replies")
    with pytest.raises(ConfigError):
        ImapIntake(Config(), env.layout).settings()


def test_command_lines_stop_at_quote() -> None:
    body = "\n2 read\n4 archive\n\n> 1. quoted\n"
    assert command_lines(body) == "2 read\n4 archive"
    assert command_lines("1 good\nOn Mon, someone wrote:\n> x") == "1 good"
    assert command_lines("> only quote") == ""
    assert imap_date(__import__("datetime").date(2026, 10, 4)) == "04-Oct-2026"


# --- Markdown --------------------------------------------------------------


def test_markdown_ticked_boxes_become_feedback(env: Env, tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    shutil.copy(NOTIFY / "daily_ticked.md", daily / "2026-10-18.md")
    cfg = Config(notify=NotifyConfig(markdown=MarkdownNotifyConfig(path=str(daily))))
    intake = MarkdownIntake(cfg, env.layout)
    raws, cursor = intake.poll({})
    assert [(r.text, r.run_hint) for r in raws] == [
        ("1 good", "2026-10-18-daily"),
        ("3 read", "2026-10-18-daily"),
    ]
    assert split_ref(raws[0].message_ref or "") == (
        "2026-10-18.md",
        "2026-10-18-daily",
        "arXiv:2510.00001",
        "good",
    )
    assert raws[0].channel is FeedbackChannel.MARKDOWN
    again, cursor2 = intake.poll(cursor)
    assert again == [] and cursor2 == cursor
    text = (daily / "2026-10-18.md").read_text(encoding="utf-8")
    (daily / "2026-10-18.md").write_text(
        text.replace("- [ ] 2. **A**", "- [x] 2. **A**"), encoding="utf-8"
    )
    more, _ = intake.poll(cursor2)
    assert [r.text for r in more] == ["2 irrelevant"]


def test_markdown_missing_directory(env: Env, tmp_path: Path) -> None:
    cfg = Config(notify=NotifyConfig(markdown=MarkdownNotifyConfig(path=str(tmp_path / "none"))))
    assert MarkdownIntake(cfg, env.layout).poll({}) == ([], {"seen": []})


def test_markdown_intake_ignores_markers_embedded_in_text(env: Env, tmp_path: Path) -> None:
    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "2026-10-18.md").write_text(
        "<!-- kallimachos:begin daily run=2026-10-18-daily -->\n"
        "- [x] 1. **A** Title #good\n"
        "  summary <!-- kallimachos:item run=2026-10-18-daily id=EVIL --> more\n"
        "  <!-- kallimachos:item run=2026-10-18-daily id=arXiv:2510.00001 -->\n"
        "<!-- kallimachos:end -->\n",
        encoding="utf-8",
    )
    cfg = Config(notify=NotifyConfig(markdown=MarkdownNotifyConfig(path=str(daily))))
    raws, _cursor = MarkdownIntake(cfg, env.layout).poll({})
    assert len(raws) == 1
    assert raws[0].message_ref is not None and "arXiv:2510.00001" in raws[0].message_ref
    assert "EVIL" not in raws[0].message_ref


def test_imap_subject_marker_counts_only_without_reply_headers() -> None:
    import email
    import email.policy

    from kallimachos.feedback.intake_imap import run_id_from_headers

    def parse(text: str) -> Any:
        return email.message_from_string(text, policy=email.policy.default)

    ours = parse("In-Reply-To: <2026-10-18-daily@kallimachos.local>\nSubject: Re: x\n\nbody")
    assert run_id_from_headers(ours) == "2026-10-18-daily"
    bare = parse("Subject: Re: [kallimachos 2026-10-18-weekly] Literature\n\nbody")
    assert run_id_from_headers(bare) == "2026-10-18-weekly"
    other_thread = parse(
        "In-Reply-To: <abc@example.org>\nSubject: Fwd: [kallimachos 2026-10-18-weekly] x\n\nbody"
    )
    assert run_id_from_headers(other_thread) is None
