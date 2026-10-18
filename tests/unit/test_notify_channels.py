"""Markdown daily, Telegram and e-mail channels; network libraries are faked."""

from __future__ import annotations

import io
import json
import smtplib
import urllib.error
import urllib.request
from email import message_from_bytes, policy
from pathlib import Path
from typing import Any

import pytest

from kallimachos.cli import main
from kallimachos.config.schema import (
    Config,
    EmailNotifyConfig,
    MarkdownNotifyConfig,
    NotifyConfig,
    TelegramNotifyConfig,
)
from kallimachos.interfaces import ApprovalRequired, ConfigError, Message, MessageItem
from kallimachos.model.none import deterministic_selection
from kallimachos.notify import base, telegram
from kallimachos.notify.email_smtp import EmailNotifier, ensure_email_confirmed
from kallimachos.notify.format import render
from kallimachos.notify.markdown_daily import MarkdownDailyNotifier, upsert_block
from kallimachos.notify.telegram import TelegramNotifier, split_text
from tests.unit.test_run_support import Env, install, make_run

TOKEN = "123456:EXAMPLEtokenEXAMPLEtoken"  # fictional, below the hygiene pattern length


# --- Markdown --------------------------------------------------------------


def _block(rid: str, body: str) -> str:
    return f"<!-- kallimachos:begin daily run={rid} -->\n{body}\n<!-- kallimachos:end -->"


def test_upsert_is_idempotent_and_keeps_user_text() -> None:
    user = "# My day\n\nPersonal notes stay here.\n"
    once = upsert_block(user, "2026-10-18-daily", _block("2026-10-18-daily", "v1"))
    assert once.startswith(user.rstrip("\n"))
    assert upsert_block(once, "2026-10-18-daily", _block("2026-10-18-daily", "v1")) == once
    twice = upsert_block(once, "2026-10-18-daily", _block("2026-10-18-daily", "v2"))
    assert "v2" in twice and "v1" not in twice and twice.count("kallimachos:begin") == 1
    assert twice.startswith(user.rstrip("\n"))
    weekly = upsert_block(twice, "2026-10-18-weekly", _block("2026-10-18-weekly", "w"))
    assert weekly.count("kallimachos:begin") == 2 and "v2" in weekly
    tail = weekly + "\nUser text after the blocks.\n"
    assert upsert_block(tail, "2026-10-18-daily", _block("2026-10-18-daily", "v3")).endswith(
        "User text after the blocks.\n"
    )
    assert upsert_block("", "r", _block("r", "x")) == _block("r", "x") + "\n"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Env:
    return install(monkeypatch, tmp_path)


def test_markdown_send_writes_daily_file(env: Env, tmp_path: Path) -> None:
    run = make_run(3)
    cfg = Config(notify=NotifyConfig(markdown=MarkdownNotifyConfig(path=str(tmp_path / "daily"))))
    msg = render(run, deterministic_selection(run, cfg.ranking), cfg)
    notifier = base.get_notifier("markdown", cfg, env.layout)
    result = notifier.send(msg)
    path = tmp_path / "daily" / "2026-10-18.md"
    assert result.ok and result.message_ref == str(path)
    text = path.read_text(encoding="utf-8")
    assert text.count("kallimachos:item") == 3
    path.write_text("Morning note.\n\n" + text, encoding="utf-8")
    notifier.send(msg)
    assert path.read_text(encoding="utf-8").startswith("Morning note.\n\n")
    assert path.read_text(encoding="utf-8").count("kallimachos:begin") == 1
    assert notifier.test().ok


def test_markdown_refuses_unmarked_user_file_without_approval(env: Env, tmp_path: Path) -> None:
    cfg = Config(notify=NotifyConfig(markdown=MarkdownNotifyConfig(path=str(tmp_path))))
    (tmp_path / "2026-10-18.md").write_text("The user's own journal.\n", encoding="utf-8")
    msg = Message(run_id="2026-10-18-daily", text="Literature\n\n1. Title", items=[])
    with pytest.raises(ApprovalRequired):
        MarkdownDailyNotifier(cfg, tmp_path).send(msg)
    assert (tmp_path / "2026-10-18.md").read_text(encoding="utf-8") == "The user's own journal.\n"
    msg.items = [MessageItem(no="1", candidate_id="arXiv:2510.00001")]
    MarkdownDailyNotifier(cfg, tmp_path, approved=True).send(msg)
    text = (tmp_path / "2026-10-18.md").read_text(encoding="utf-8")
    assert text.startswith("The user's own journal.\n")
    assert (
        "- [ ] 1. Title\n  <!-- kallimachos:item run=2026-10-18-daily id=arXiv:2510.00001 -->"
        in text
    )


def test_default_markdown_directory_and_unknown_channel(env: Env) -> None:
    assert base.markdown_directory(Config(), env.layout) == env.layout.daily
    with pytest.raises(Exception, match="unknown notification channel"):
        base.get_notifier("pigeon", Config(), env.layout)
    assert [n.name for n in base.get_notifiers(Config(), env.layout)] == ["markdown"]


# --- Telegram --------------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class FakeTelegram:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.fail_with: Exception | None = None
        self.next_id = 100

    def __call__(self, req: urllib.request.Request, timeout: float = 0) -> FakeResponse:
        assert isinstance(req.data, bytes)
        payload = json.loads(req.data.decode("utf-8"))
        self.requests.append((req.full_url, payload))
        if self.fail_with is not None:
            raise self.fail_with
        method = req.full_url.rsplit("/", 1)[-1]
        if method == "sendMessage":
            self.next_id += 1
            result: Any = {"message_id": self.next_id}
        elif method == "getMe":
            result = {"username": "example_bot"}
        else:
            result = []
        return FakeResponse(json.dumps({"ok": True, "result": result}).encode())


def _tg_config() -> Config:
    return Config(
        notify=NotifyConfig(
            channels=["telegram"],
            telegram=TelegramNotifyConfig(bot_token_ref="env:EXAMPLE_TG", chat_id=4242),
        )
    )


def test_telegram_sends_plain_text_and_remembers_ids(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.secrets["env:EXAMPLE_TG"] = TOKEN
    fake = FakeTelegram()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    cfg = _tg_config()
    run = make_run(2)
    msg = render(run, deterministic_selection(run, cfg.ranking), cfg)
    result = base.get_notifier("telegram", cfg, env.layout).send(msg)
    assert result.ok and result.message_ref == "101"
    url, payload = fake.requests[0]
    assert url.endswith("/sendMessage") and TOKEN in url
    assert payload == {"chat_id": "4242", "text": msg.text, "disable_web_page_preview": True}
    assert "parse_mode" not in payload
    assert telegram.load_sent(env.layout.state) == {"101": "2026-10-18-daily"}


def test_telegram_error_never_contains_token(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.secrets["env:EXAMPLE_TG"] = TOKEN
    fake = FakeTelegram()
    body = json.dumps({"ok": False, "description": "Unauthorized"}).encode()
    fake.fail_with = urllib.error.HTTPError(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        401,
        "x",
        {},
        io.BytesIO(body),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    result = TelegramNotifier(_tg_config()).send(Message(run_id="2026-10-18-daily", text="hi"))
    assert not result.ok and result.error and "Unauthorized" in result.error
    assert TOKEN not in result.error
    fake.fail_with = urllib.error.URLError(f"cannot reach bot{TOKEN}")
    check = TelegramNotifier(_tg_config()).test()
    assert not check.ok and TOKEN not in check.message


def test_telegram_test_and_missing_config(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.secrets["env:EXAMPLE_TG"] = TOKEN
    monkeypatch.setattr(urllib.request, "urlopen", FakeTelegram())
    check = TelegramNotifier(_tg_config()).test()
    assert check.ok and check.details == {"bot": "example_bot"}
    cfg = _tg_config()
    cfg.notify.telegram.chat_id = None
    with pytest.raises(ConfigError):
        TelegramNotifier(cfg).send(Message(run_id="2026-10-18-daily", text="x"))


def test_split_text_never_drops_text() -> None:
    blocks = ["\U0001f4da" * 900 + f" block {i}" for i in range(5)]
    text = "\n\n".join(blocks)
    chunks = split_text(text, limit=4096)
    assert len(chunks) > 1
    assert all(telegram.utf16_len(c) <= 4096 for c in chunks)
    assert "\n\n".join(chunks) == text
    assert split_text("short") == ["short"]


# --- e-mail ----------------------------------------------------------------


class FakeSMTP:
    instances: list[FakeSMTP] = []

    def __init__(self, host: str, port: int, timeout: float = 0, context: Any = None) -> None:
        self.host, self.port, self.context = host, port, context
        self.calls: list[str] = []
        self.sent: list[Any] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append("quit")

    def starttls(self, context: Any = None) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(f"login:{user}:{password}")

    def send_message(self, msg: Any) -> None:
        self.sent.append(msg)


def _email_config(**kw: Any) -> Config:
    email = EmailNotifyConfig(
        smtp_host="smtp.example.org",
        username="reader@example.org",
        password_ref="keyring:kallimachos/smtp",
        from_="reader@example.org",
        to=["reader@example.org", "colleague@example.org"],
        **kw,
    )
    return Config(notify=NotifyConfig(channels=["markdown", "email"], email=email))


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def test_email_starttls_headers_and_alternative(env: Env, smtp: type[FakeSMTP]) -> None:
    env.secrets["keyring:kallimachos/smtp"] = "app-password"
    cfg = _email_config()
    run = make_run(2)
    msg = render(run, deterministic_selection(run, cfg.ranking), cfg)
    result = EmailNotifier(cfg).send(msg)
    assert result.ok and result.message_ref == "<2026-10-18-daily@kallimachos.local>"
    server = smtp.instances[0]
    assert (server.host, server.port) == ("smtp.example.org", 587)
    assert server.calls == ["starttls", "login:reader@example.org:app-password", "quit"]
    sent = message_from_bytes(server.sent[0].as_bytes(), policy=policy.default)
    assert sent["Message-ID"] == "<2026-10-18-daily@kallimachos.local>"
    assert sent["Subject"] == "[kallimachos 2026-10-18-daily] Literature — 18 Oct 2026"
    assert sent["To"] == "reader@example.org, colleague@example.org"
    assert sent["Reply-To"] == "reader@example.org"
    assert sent.get_body(("plain",)).get_content().startswith("Literature")
    assert "<a href=" in sent.get_body(("html",)).get_content()


def test_email_ssl_and_failure(
    env: Env, smtp: type[FakeSMTP], monkeypatch: pytest.MonkeyPatch
) -> None:
    env.secrets["keyring:kallimachos/smtp"] = "pw"
    cfg = _email_config(security="ssl", smtp_port=465)
    assert EmailNotifier(cfg).test().ok
    assert smtp.instances[0].context is not None and "starttls" not in smtp.instances[0].calls

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise smtplib.SMTPConnectError(421, b"busy")

    monkeypatch.setattr(smtplib, "SMTP_SSL", refuse)
    result = EmailNotifier(cfg).send(Message(run_id="2026-10-18-daily", text="x"))
    assert not result.ok and "SMTPConnectError" in (result.error or "")
    incomplete = Config(notify=NotifyConfig(email=EmailNotifyConfig(smtp_host="smtp.example.org")))
    assert not EmailNotifier(incomplete).test().ok


def test_email_gate_and_confirm_command(
    env: Env, smtp: type[FakeSMTP], capsys: pytest.CaptureFixture[str]
) -> None:
    env.secrets["keyring:kallimachos/smtp"] = "pw"
    env.config = _email_config()
    with pytest.raises(ApprovalRequired):
        ensure_email_confirmed(env.config)
    assert main(["notify", "test", "--channel", "email"]) == 0
    assert "confirm-email" in capsys.readouterr().out
    assert env.written_config is None
    assert main(["notify", "confirm-email"]) == 0
    assert env.written_config is not None
    assert env.written_config.notify.email.confirmed_at is not None
    ensure_email_confirmed(env.written_config)  # no longer raises


def test_confirm_email_without_email_config_is_usage_error(env: Env) -> None:
    assert main(["notify", "confirm-email"]) == 2
    assert env.written_config is None
    ensure_email_confirmed(Config())  # e-mail not a channel: nothing to confirm


def test_notify_test_exit_3_when_a_channel_fails(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env.config = _tg_config()
    env.config.notify.channels = ["markdown", "telegram"]
    env.secrets["env:EXAMPLE_TG"] = TOKEN
    fake = FakeTelegram()
    fake.fail_with = urllib.error.URLError("offline")
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert main(["--json", "notify", "test"]) == 3
    data = json.loads(capsys.readouterr().out)
    assert data["markdown"]["ok"] is True and data["telegram"]["ok"] is False


def test_confirm_email_needs_a_sent_test_mail(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.secrets["keyring:kallimachos/smtp"] = "pw"
    env.config = _email_config()
    assert main(["notify", "confirm-email"]) == 2
    assert "notify test --channel email" in capsys.readouterr().err
    assert env.written_config is None
