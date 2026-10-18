"""IMAP replies: ``In-Reply-To`` / subject ``[kallimachos <run_id>]`` resolve the run; first body
lines are the command language.

Mailbox settings, in this order: explicit constructor arguments, ``notify.email.imap_host`` (with
``imap_port`` / ``imap_folder`` and the SMTP credentials), the ``sources.scholar_imap`` account
when it has a host, else the SMTP account with ``smtp.`` replaced by ``imap.`` in the host name.
The mailbox is opened read-only; nothing is moved or flagged.

Trust model. Only replies whose ``From`` address is in ``notify.email.to`` / ``notify.email.from``
are accepted; ``From`` is a header anyone can forge, so this filter stops strangers, not a
targeted sender who knows the user's address (what such a reply can do is limited to feedback
tags and open-access PDF downloads). The run is resolved from ``In-Reply-To`` / ``References``
carrying our own ``Message-ID`` (``<run_id@kallimachos.local>``); the subject fallback
``[kallimachos <run_id>]`` is used only for a mail that carries no reply headers at all (a client
that dropped them), never for a reply to some other thread that merely mentions a run in its
subject. The cursor is ``{"uidvalidity": int, "last_uid": int}``; the first poll looks back
:data:`FIRST_POLL_DAYS` days.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import email
import email.policy
import email.utils
import imaplib
import re
import ssl
from collections.abc import Mapping
from email.message import EmailMessage
from typing import Any

from kallimachos import secrets
from kallimachos.config.schema import Config
from kallimachos.interfaces import RUN_ID_PATTERN, ConfigError, FeedbackChannel, RawFeedback
from kallimachos.paths import DataLayout

FIRST_POLL_DAYS = 14
TIMEOUT_S = 60.0
_RID = RUN_ID_PATTERN.strip("^$")
_MSGID_RE = re.compile(r"<(" + _RID + r")@kallimachos\.local>")
_SUBJECT_RE = re.compile(r"\[kallimachos (" + _RID + r")\]")
_QUOTE_START = re.compile(
    r"^(>|On .+wrote:\s*$|-- ?$|-{2,}\s*Original Message|From:\s|Sent:\s|_{5,})", re.IGNORECASE
)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def run_id_from_headers(msg: EmailMessage) -> str | None:
    """Run id from our ``Message-ID`` in ``In-Reply-To`` / ``References``; the subject marker
    counts only when the mail carries neither header."""
    has_reply_headers = False
    for header in ("In-Reply-To", "References"):
        value = str(msg.get(header, "")).strip()
        if value:
            has_reply_headers = True
        m = _MSGID_RE.search(value)
        if m:
            return m.group(1)
    if has_reply_headers:
        return None
    m = _SUBJECT_RE.search(str(msg.get("Subject", "")))
    return m.group(1) if m else None


def command_lines(body: str) -> str:
    """Leading lines of a reply up to the first blank line after content or the quoted part."""
    lines: list[str] = []
    for line in body.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if _QUOTE_START.match(stripped):
            break
        if not stripped:
            if lines:
                break
            continue
        lines.append(stripped)
    return "\n".join(lines)


def plain_body(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain",))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError):
        return ""
    return content if isinstance(content, str) else ""


def imap_date(day: _dt.date) -> str:
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


class ImapIntake:
    """Implements :class:`kallimachos.interfaces.FeedbackIntake`."""

    name = "imap"

    def __init__(
        self,
        config: Config,
        layout: DataLayout,
        *,
        host: str | None = None,
        port: int | None = None,
        folder: str | None = None,
        user: str | None = None,
        password_ref: str | None = None,
    ) -> None:
        self.config = config
        self.layout = layout
        self.host = host
        self.port = port
        self.folder = folder
        self.user = user
        self.password_ref = password_ref

    def settings(self) -> tuple[str, int, str, str, str]:
        """(host, port, folder, user, password_ref) following the module docstring order."""
        email_cfg = self.config.notify.email
        scholar = self.config.sources.scholar_imap
        if self.host:
            host, port, folder = self.host, self.port or 993, self.folder or "INBOX"
            user = self.user or email_cfg.username
            pw_ref = self.password_ref or email_cfg.password_ref
        elif email_cfg.imap_host:
            host = email_cfg.imap_host
            port = self.port or email_cfg.imap_port
            folder = self.folder or email_cfg.imap_folder
            user = self.user or email_cfg.username
            pw_ref = self.password_ref or email_cfg.password_ref
        elif scholar.host:
            host, port, folder = scholar.host, scholar.port, self.folder or scholar.folder
            user = self.user or (
                secrets.resolve_ref(scholar.user_ref) if scholar.user_ref else None
            )
            pw_ref = self.password_ref or scholar.password_ref
        elif email_cfg.smtp_host:
            smtp = email_cfg.smtp_host
            host = "imap." + smtp[5:] if smtp.startswith("smtp.") else smtp
            port, folder = self.port or 993, self.folder or "INBOX"
            user = self.user or email_cfg.username
            pw_ref = self.password_ref or email_cfg.password_ref
        else:
            raise ConfigError("no IMAP account: set notify.email.smtp_host or sources.scholar_imap")
        if not user or not pw_ref:
            raise ConfigError("IMAP feedback needs a username and a password_ref")
        return host, port, folder, user, pw_ref

    def allowed_senders(self) -> set[str]:
        cfg = self.config.notify.email
        addrs = [*cfg.to, *([cfg.from_] if cfg.from_ else [])]
        return {email.utils.parseaddr(a)[1].lower() for a in addrs if a}

    def poll(self, cursor: Mapping[str, Any]) -> tuple[list[RawFeedback], dict[str, Any]]:
        host, port, folder, user, pw_ref = self.settings()
        allowed = self.allowed_senders()
        conn = imaplib.IMAP4_SSL(
            host, port, ssl_context=ssl.create_default_context(), timeout=TIMEOUT_S
        )
        try:
            conn.login(user, secrets.resolve_ref(pw_ref))
            conn.select(folder, readonly=True)
            _, validity_data = conn.response("UIDVALIDITY")
            validity = int(validity_data[0]) if validity_data and validity_data[0] else 0
            last = int(cursor.get("last_uid", 0) or 0)
            if cursor.get("uidvalidity") not in (None, validity):
                last = 0
            if last:
                _, data = conn.uid("SEARCH", f"UID {last + 1}:*")
            else:
                since = _dt.date.today() - _dt.timedelta(days=FIRST_POLL_DAYS)
                _, data = conn.uid("SEARCH", "SINCE", imap_date(since))
            uids = sorted(int(x) for x in (data[0] or b"").split() if int(x) > last)
            out: list[RawFeedback] = []
            for uid in uids:
                _, fetched = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
                raw = next(
                    (part[1] for part in fetched if isinstance(part, tuple) and len(part) > 1),
                    None,
                )
                if not isinstance(raw, bytes):
                    continue
                parsed = self._parse(raw, allowed)
                if parsed is not None:
                    out.append(parsed)
            new_last = max([last, *uids])
        finally:
            with contextlib.suppress(imaplib.IMAP4.error, OSError):
                conn.logout()
        return out, {"uidvalidity": validity, "last_uid": new_last}

    def _parse(self, raw: bytes, allowed: set[str]) -> RawFeedback | None:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        assert isinstance(msg, EmailMessage)
        sender = email.utils.parseaddr(str(msg.get("From", "")))[1].lower()
        if allowed and sender not in allowed:
            return None
        rid = run_id_from_headers(msg)
        if rid is None:
            return None
        text = command_lines(plain_body(msg))
        if not text:
            return None
        date_header = msg.get("Date")
        try:
            at = email.utils.parsedate_to_datetime(str(date_header)) if date_header else None
        except (TypeError, ValueError):
            at = None
        if at is None or at.tzinfo is None:
            at = _dt.datetime.now(_dt.UTC)
        return RawFeedback(
            at=at,
            channel=FeedbackChannel.IMAP,
            text=text,
            run_hint=rid,
            message_ref=str(msg.get("Message-ID", "")) or None,
        )
