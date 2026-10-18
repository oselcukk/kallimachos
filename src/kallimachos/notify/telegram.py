"""Telegram Bot API notifier: ``sendMessage`` without parse_mode, plain URLs.

The bot token sits in the URL path (Bot API design), so Telegram calls do not go through the shared
``Http`` client, whose URLs end up in logs and cache keys; :func:`call_api` posts JSON with
``urllib`` and redacts the token from every error it raises. Sent message ids are remembered per
run so a ``reply_to_message`` in ``feedback poll`` resolves the run.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from kallimachos import secrets
from kallimachos.config.schema import Config
from kallimachos.interfaces import CheckResult, ConfigError, HttpClient, Message, NotifyResult
from kallimachos.notify.base import NotifierBase
from kallimachos.notify.budget import TELEGRAM_HARD_LIMIT, utf16_len
from kallimachos.state import store

API_URL = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT_S = 30.0
SENT_FILE = "telegram_sent.json"
SENT_KEEP = 500


class TelegramError(Exception):
    """A Bot API call failed; the message never contains the token."""


def call_api(
    token: str, method: str, payload: dict[str, Any], *, timeout: float = TIMEOUT_S
) -> Any:
    """POST ``payload`` as JSON to ``method``; return ``result`` or raise :class:`TelegramError`."""
    req = urllib.request.Request(
        API_URL.format(token=token, method=method),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read() or b""
        finally:
            exc.close()
        detail = _description(body) or f"HTTP {exc.code}"
        raise TelegramError(_redact(f"{method}: {detail}", token)) from None
    except (urllib.error.URLError, OSError) as exc:
        raise TelegramError(_redact(f"{method}: {exc}", token)) from None
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TelegramError(f"{method}: response is not JSON") from None
    if not isinstance(data, dict) or not data.get("ok"):
        reason = data.get("description") if isinstance(data, dict) else None
        raise TelegramError(_redact(f"{method}: {reason or 'not ok'}", token))
    return data.get("result")


def _description(body: bytes) -> str | None:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    desc = data.get("description") if isinstance(data, dict) else None
    return str(desc) if desc else None


def _redact(text: str, token: str) -> str:
    return text.replace(token, "<token>") if token else text


def bot_token(config: Config) -> str:
    ref = config.notify.telegram.bot_token_ref
    if not ref:
        raise ConfigError("notify.telegram.bot_token_ref is not set")
    return secrets.resolve_ref(ref)


def chat_id(config: Config) -> str:
    value = config.notify.telegram.chat_id
    if value is None or str(value) == "":
        raise ConfigError("notify.telegram.chat_id is not set")
    return str(value)


def split_text(text: str, limit: int = TELEGRAM_HARD_LIMIT) -> list[str]:
    """Split at blank lines (then lines) so every chunk fits ``limit`` UTF-16 units. The budget
    normally keeps a message in one piece; this only guards the hard limit, never drops text."""
    if utf16_len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for unit in _units(text, limit):
        candidate = f"{current}\n\n{unit}" if current else unit
        if utf16_len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = unit
    if current:
        chunks.append(current)
    return chunks


def _units(text: str, limit: int) -> list[str]:
    out: list[str] = []
    for block in text.split("\n\n"):
        if utf16_len(block) <= limit:
            out.append(block)
            continue
        for line in block.split("\n"):
            while utf16_len(line) > limit:
                out.append(line[: limit // 2])
                line = line[limit // 2 :]
            out.append(line)
    return out


def sent_file(state_dir: Path) -> Path:
    """``DataLayout.telegram_sent_file`` for ``state_dir = layout.state``."""
    return state_dir / SENT_FILE


def load_sent(state_dir: Path) -> dict[str, str]:
    data = store.read_json(sent_file(state_dir), {})
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def remember_sent(state_dir: Path, message_ids: list[int], run_id: str) -> None:
    sent = load_sent(state_dir)
    for mid in message_ids:
        sent[str(mid)] = run_id
    if len(sent) > SENT_KEEP:
        sent = dict(list(sent.items())[-SENT_KEEP:])
    store.write_json_atomic(sent_file(state_dir), sent)


class TelegramNotifier(NotifierBase):
    name = "telegram"

    def __init__(
        self, config: Config, http: HttpClient | None = None, *, state_dir: Path | None = None
    ) -> None:
        self.config = config
        self.http = http  # unused: the token-bearing URL must not pass through the shared client
        self.state_dir = state_dir

    def _send_text(self, text: str) -> list[int]:
        token = bot_token(self.config)
        target = chat_id(self.config)
        ids: list[int] = []
        for chunk in split_text(text):
            result = call_api(
                token,
                "sendMessage",
                {"chat_id": target, "text": chunk, "disable_web_page_preview": True},
            )
            if isinstance(result, dict) and isinstance(result.get("message_id"), int):
                ids.append(result["message_id"])
        return ids

    def send(self, message: Message) -> NotifyResult:
        try:
            ids = self._send_text(message.text)
        except TelegramError as exc:
            return NotifyResult(channel=self.name, ok=False, error=str(exc))
        if self.state_dir is not None and ids:
            remember_sent(self.state_dir, ids, message.run_id)
        return NotifyResult(
            channel=self.name, ok=True, message_ref=",".join(str(i) for i in ids) or None
        )

    def test(self) -> CheckResult:
        try:
            token = bot_token(self.config)
            me = call_api(token, "getMe", {})
            self._send_text("Kallimachos test message: notifications reach this chat.")
        except (TelegramError, ConfigError) as exc:
            return CheckResult(ok=False, message=str(exc))
        username = me.get("username") if isinstance(me, dict) else None
        return CheckResult(
            ok=True, message="test message sent", details={"bot": username} if username else {}
        )
