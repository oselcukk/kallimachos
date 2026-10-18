"""Telegram ``getUpdates`` with ``update_id`` offset in the cursor; ``reply_to_message`` resolves
the run.

Only messages from the configured ``notify.telegram.chat_id`` are taken; everything else is
skipped but still advances the offset. A reply to a message the notifier sent carries the run id
(``state/telegram_sent.json``); other messages get no run hint and the caller resolves the run
from the date and the item numbers.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Mapping
from typing import Any

from kallimachos.config.schema import Config
from kallimachos.interfaces import FeedbackChannel, RawFeedback
from kallimachos.notify.telegram import bot_token, call_api, chat_id, load_sent
from kallimachos.paths import DataLayout


class TelegramIntake:
    """Implements :class:`kallimachos.interfaces.FeedbackIntake`."""

    name = "telegram"

    def __init__(self, config: Config, layout: DataLayout) -> None:
        self.config = config
        self.layout = layout

    def poll(self, cursor: Mapping[str, Any]) -> tuple[list[RawFeedback], dict[str, Any]]:
        offset = int(cursor.get("offset", 0) or 0)
        token = bot_token(self.config)
        chat = chat_id(self.config)
        payload: dict[str, Any] = {"timeout": 0, "allowed_updates": ["message"]}
        if offset:
            payload["offset"] = offset
        updates = call_api(token, "getUpdates", payload)
        sent = load_sent(self.layout.state)
        out: list[RawFeedback] = []
        next_offset = offset
        for update in updates if isinstance(updates, list) else []:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                next_offset = max(next_offset, update_id + 1)
            msg = update.get("message")
            if not isinstance(msg, dict):
                continue
            if str((msg.get("chat") or {}).get("id")) != chat:
                continue
            text = msg.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            reply = msg.get("reply_to_message")
            hint = sent.get(str(reply.get("message_id"))) if isinstance(reply, dict) else None
            date = msg.get("date")
            at = (
                _dt.datetime.fromtimestamp(date, _dt.UTC)
                if isinstance(date, int)
                else _dt.datetime.now(_dt.UTC)
            )
            out.append(
                RawFeedback(
                    at=at,
                    channel=FeedbackChannel.TELEGRAM,
                    text=text.strip(),
                    run_hint=hint,
                    message_ref=str(update_id),
                )
            )
        return out, {"offset": next_offset}
