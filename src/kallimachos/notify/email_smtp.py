"""SMTP notifier: STARTTLS/SSL, plain + HTML, ``Message-ID`` carries the run id.

``From`` is the user's own account (``notify.email.from``), ``To`` the configured recipient list,
``Reply-To`` the sender so replies come back to the mailbox the IMAP intake reads. The password is
only ever resolved from ``notify.email.password_ref`` (keyring or environment variable).

``schedule install`` refuses while ``notify.email.confirmed_at`` is null (exit 5): the scheduler
code calls :func:`ensure_email_confirmed`.
"""

from __future__ import annotations

import datetime as _dt
import email.utils
import re
import smtplib
import ssl
from email.message import EmailMessage

from kallimachos import secrets
from kallimachos.config.schema import Config, EmailNotifyConfig
from kallimachos.interfaces import (
    RUN_ID_PATTERN,
    ApprovalRequired,
    CheckResult,
    ConfigError,
    Message,
    NotifyResult,
)
from kallimachos.notify.base import NotifierBase

SMTP_TIMEOUT_S = 60.0
_RUN_ID_RE = re.compile(RUN_ID_PATTERN)


def message_id(run_id: str) -> str:
    """``<{run_id}@kallimachos.local>``."""
    return f"<{run_id}@kallimachos.local>"


def subject(run_id: str, header: str) -> str:
    """``[kallimachos {run_id}] {header}``."""
    return f"[kallimachos {run_id}] {header}"


def ensure_email_confirmed(config: Config) -> None:
    """Raise :class:`ApprovalRequired` (exit 5) when e-mail is a notification channel and the test
    mail has not been confirmed with ``notify confirm-email``."""
    if "email" in config.notify.channels and config.notify.email.confirmed_at is None:
        raise ApprovalRequired(
            "e-mail notifications are not confirmed: run "
            "`kallimachos notify test --channel email`, check that the test mail arrived, "
            "then `kallimachos notify confirm-email`"
        )


def _check_config(cfg: EmailNotifyConfig) -> None:
    missing = [
        name
        for name, value in (("smtp_host", cfg.smtp_host), ("from", cfg.from_), ("to", cfg.to))
        if not value
    ]
    if missing:
        raise ConfigError(f"notify.email is incomplete: missing {', '.join(missing)}")


def build_email(
    cfg: EmailNotifyConfig,
    *,
    subject_line: str,
    text: str,
    html: str | None,
    msg_id: str,
) -> EmailMessage:
    _check_config(cfg)
    assert cfg.from_ is not None
    msg = EmailMessage()
    msg["From"] = cfg.from_
    msg["To"] = ", ".join(cfg.to)
    msg["Reply-To"] = cfg.from_
    msg["Subject"] = subject_line
    msg["Message-ID"] = msg_id
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def deliver(cfg: EmailNotifyConfig, msg: EmailMessage) -> None:
    """Connect (SSL or STARTTLS), log in when a username is set, send."""
    _check_config(cfg)
    assert cfg.smtp_host is not None
    context = ssl.create_default_context()
    server: smtplib.SMTP
    if cfg.security == "ssl":
        server = smtplib.SMTP_SSL(
            cfg.smtp_host, cfg.smtp_port, timeout=SMTP_TIMEOUT_S, context=context
        )
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=SMTP_TIMEOUT_S)
    with server:
        if cfg.security == "starttls":
            server.starttls(context=context)
        if cfg.username:
            if not cfg.password_ref:
                raise ConfigError("notify.email.username is set but password_ref is missing")
            server.login(cfg.username, secrets.resolve_ref(cfg.password_ref))
        server.send_message(msg)


class EmailNotifier(NotifierBase):
    name = "email"

    def __init__(self, config: Config) -> None:
        self.config = config

    def send(self, message: Message) -> NotifyResult:
        cfg = self.config.notify.email
        header = message.text.split("\n", 1)[0]
        msg_id = (
            message_id(message.run_id)
            if _RUN_ID_RE.match(message.run_id)
            else email.utils.make_msgid(domain="kallimachos.local")
        )
        try:
            msg = build_email(
                cfg,
                subject_line=message.subject or subject(message.run_id, header),
                text=message.text,
                html=message.html,
                msg_id=msg_id,
            )
            deliver(cfg, msg)
        except ConfigError:
            raise
        except (OSError, smtplib.SMTPException) as exc:
            return NotifyResult(channel=self.name, ok=False, error=f"{type(exc).__name__}: {exc}")
        return NotifyResult(channel=self.name, ok=True, message_ref=msg_id)

    def test(self) -> CheckResult:
        """Send the test mail. The user then says whether it arrived; ``notify confirm-email``
        records ``confirmed_at``."""
        cfg = self.config.notify.email
        stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%d%H%M%S")
        msg_id = f"<test-{stamp}@kallimachos.local>"
        text = (
            "This is a Kallimachos test message.\n"
            "If you can read it, tell your agent so it runs `kallimachos notify confirm-email`."
        )
        try:
            msg = build_email(
                cfg,
                subject_line="[kallimachos test] notification check",
                text=text,
                html=None,
                msg_id=msg_id,
            )
            deliver(cfg, msg)
        except ConfigError as exc:
            return CheckResult(ok=False, message=str(exc))
        except (OSError, smtplib.SMTPException) as exc:
            return CheckResult(ok=False, message=f"{type(exc).__name__}: {exc}")
        return CheckResult(
            ok=True,
            message=f"test e-mail sent to {len(cfg.to)} recipient(s); after it arrives run "
            "`kallimachos notify confirm-email`",
            details={"message_id": msg_id, "confirmed": cfg.confirmed_at is not None},
        )
