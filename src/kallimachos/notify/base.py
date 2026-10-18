"""Notifier factory and shared base."""

from __future__ import annotations

import abc
from pathlib import Path

from kallimachos.config.schema import Config
from kallimachos.interfaces import CheckResult, Message, Notifier, NotifyResult, UsageError
from kallimachos.paths import DataLayout

CHANNELS: tuple[str, ...] = ("markdown", "telegram", "email")


class NotifierBase(abc.ABC):
    """Common base; concrete channels implement both methods."""

    name: str = ""

    @abc.abstractmethod
    def send(self, message: Message) -> NotifyResult: ...

    @abc.abstractmethod
    def test(self) -> CheckResult: ...


def markdown_directory(config: Config, layout: DataLayout) -> Path:
    """``notify.markdown.path`` (``~`` expanded) or ``<data_dir>/daily``."""
    raw = config.notify.markdown.path
    return Path(raw).expanduser() if raw else layout.daily


def get_notifiers(config: Config, layout: DataLayout, *, approved: bool = False) -> list[Notifier]:
    """Instances for ``config.notify.channels`` in order."""
    return [
        get_notifier(name, config, layout, approved=approved) for name in config.notify.channels
    ]


def get_notifier(
    name: str, config: Config, layout: DataLayout, *, approved: bool = False
) -> Notifier:
    """``approved`` is the ``--yes`` flag: permission to add the marked block to a Markdown file
    the user wrote."""
    if name == "markdown":
        from kallimachos.notify.markdown_daily import MarkdownDailyNotifier

        return MarkdownDailyNotifier(config, markdown_directory(config, layout), approved=approved)
    if name == "telegram":
        from kallimachos.notify.telegram import TelegramNotifier

        return TelegramNotifier(config, state_dir=layout.state)
    if name == "email":
        from kallimachos.notify.email_smtp import EmailNotifier

        return EmailNotifier(config)
    raise UsageError(
        f"unknown notification channel {name!r} (expected one of {', '.join(CHANNELS)})"
    )
