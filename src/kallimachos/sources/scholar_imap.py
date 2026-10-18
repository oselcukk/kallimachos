"""Google Scholar alerts over IMAP source.

reads alert e-mails from the user's mailbox (sender_filter configurable); never requests Scholar
itself.
Fixtures: ``tests/fixtures/scholar_imap/``.

The mailbox is opened read-only. Links in alerts are Scholar redirects
(``scholar.google.com/scholar_url?url=<target>``); the target is taken from the query string and
no request is ever sent to Scholar (the ``http`` argument is unused). The sender address is not
documented by Google, so it is configurable and ``test`` warns when the box holds no
mail from it. When the alert markup changes and an alert yields no items, a warning is recorded
instead of silently dropping it. Alert text is data, never instructions.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import email
import email.policy
import email.utils
import imaplib
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

from kallimachos.config.schema import ScholarImapConfig
from kallimachos.interfaces import (
    Author,
    HttpClient,
    Ident,
    OaCandidate,
    Record,
    SourceTest,
    Window,
    WorkKind,
)
from kallimachos.profile.schema import Profile
from kallimachos.sources.base import BaseSource, SecretResolver, clean_text, find_arxiv, find_doi

SEARCH_DAYS_FOR_TEST = 60
MAX_MESSAGES = 200


class ImapLike(Protocol):
    """The ``imaplib.IMAP4`` subset used here (tests pass a fake)."""

    def login(self, user: str, password: str) -> Any: ...

    def select(self, mailbox: str = ..., readonly: bool = ...) -> Any: ...

    def search(self, charset: str | None, *criteria: str) -> Any: ...

    def fetch(self, message_set: str, message_parts: str) -> Any: ...

    def logout(self) -> Any: ...


ImapFactory = Callable[[str, int], ImapLike]


def default_imap_factory(host: str, port: int) -> ImapLike:
    return imaplib.IMAP4_SSL(host, port)


@dataclass
class AlertItem:
    title: str = ""
    href: str = ""
    byline: str = ""
    snippet: str = ""
    pdf_marker: bool = False


class _AlertParser(HTMLParser):
    """Scholar alert layout: ``h3 > a.gse_alrt_title``, a byline ``div``, ``div.gse_alrt_sni``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[AlertItem] = []
        self._in_title = False
        self._capture: str | None = None  # "byline" | "snippet"
        self._depth = 0
        self._pending_pdf = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        classes = a.get("class", "").split()
        if tag == "h3":
            self._pending_pdf = False
        if tag == "a" and "gse_alrt_title" in classes:
            self.items.append(AlertItem(href=a.get("href", ""), pdf_marker=self._pending_pdf))
            self._in_title = True
            return
        if tag == "div":
            if self._capture is not None:
                self._depth += 1
                return
            if not self.items:
                return
            item = self.items[-1]
            if "gse_alrt_sni" in classes and not item.snippet:
                self._capture, self._depth = "snippet", 1
            elif not item.byline and not item.snippet:
                self._capture, self._depth = "byline", 1
        if tag == "br" and self._capture is not None:
            self._append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title:
            self._in_title = False
        if tag == "div" and self._capture is not None:
            self._depth -= 1
            if self._depth == 0:
                self._capture = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.items[-1].title += data
        elif self._capture is not None:
            self._append(data)
        elif "[PDF]" in data:
            self._pending_pdf = True

    def _append(self, data: str) -> None:
        item = self.items[-1]
        if self._capture == "byline":
            item.byline += data
        else:
            item.snippet += data


def target_url(href: str) -> str:
    """The real link behind a Scholar redirect (no request is made)."""
    parts = urlsplit(href)
    if parts.hostname and parts.hostname.endswith("scholar.google.com"):
        target = parse_qs(parts.query).get("url")
        return target[0] if target else ""
    return href


def split_byline(byline: str) -> tuple[list[str], str | None]:
    """``"EA Author, EB Author - Venue, 2026"`` -> (authors, venue)."""
    text = clean_text(byline)
    authors_part, _sep, venue_part = text.partition(" - ")
    authors = [a.strip(" .…") for a in authors_part.replace("…", "").split(",") if a.strip(" .…")]
    venue = re.sub(r",?\s*\d{4}\s*$", "", venue_part).strip(" ,") or None
    return authors, venue


def parse_alert(html: str) -> list[AlertItem]:
    parser = _AlertParser()
    parser.feed(html)
    parser.close()
    return [i for i in parser.items if clean_text(i.title)]


def alert_html(msg: EmailMessage) -> str | None:
    part = msg.get_body(preferencelist=("html",))
    if part is None:
        return None
    content = part.get_content()
    return content if isinstance(content, str) else None


def item_record(item: AlertItem, received: _dt.date | None) -> Record:
    url = target_url(item.href)
    authors, venue = split_byline(item.byline)
    doi = find_doi(url)
    arxiv = find_arxiv(url, item.byline)
    snippet = clean_text(item.snippet).rstrip(" …").strip() or None
    return Record(
        title=clean_text(item.title),
        authors=[Author(name=a) for a in authors],
        abstract=snippet,
        abstract_source="scholar_alert_snippet" if snippet else None,
        venue=venue,
        created=received,
        doi=doi,
        arxiv_id=arxiv[0] if arxiv else None,
        url=url or None,
        kind=WorkKind.PREPRINT if arxiv and not doi else WorkKind.UNKNOWN,
        sources={"scholar_imap"},
        queries={"scholar_imap:alert"},
    )


def _safe(value: str) -> str:
    return re.sub(r'[\s"()\\]', "", value)


class ScholarImapSource(BaseSource):
    name = "scholar_imap"
    needs_key = True
    default_interval_s = 0.0
    hosts = ()

    def __init__(
        self,
        config: object,
        *,
        resolve_secret: SecretResolver | None = None,
        imap_factory: ImapFactory = default_imap_factory,
    ) -> None:
        """``config`` is ``Config.sources.scholar_imap``."""
        super().__init__(config, resolve_secret=resolve_secret)
        self.config = config if isinstance(config, ScholarImapConfig) else ScholarImapConfig()
        self._imap_factory = imap_factory

    def key_refs(self) -> dict[str, str | None]:
        return {"user": self.config.user_ref, "password": self.config.password_ref}

    # -- mailbox -------------------------------------------------------------------------------

    def _open(self) -> ImapLike | None:
        cfg = self.config
        if not cfg.host:
            self.error("scholar_imap: no IMAP host configured")
            return None
        user = self.secret(cfg.user_ref, "user")
        password = self.secret(cfg.password_ref, "password")
        if not user or not password:
            self.error("scholar_imap: IMAP user or password reference missing")
            return None
        try:
            box = self._imap_factory(cfg.host, cfg.port)
            box.login(user, password)
            box.select(cfg.folder, readonly=True)
        except (OSError, imaplib.IMAP4.error) as exc:
            self.error(f"scholar_imap: cannot open mailbox: {type(exc).__name__}")
            return None
        return box

    def _messages(self, box: ImapLike, since: _dt.date) -> list[EmailMessage]:
        criteria = f'(FROM "{_safe(self.config.sender_filter)}" SINCE {since:%d-%b-%Y})'
        self.requests += 1
        typ, data = box.search(None, criteria)
        if typ != "OK" or not data:
            return []
        numbers = (data[0] or b"").split()[-MAX_MESSAGES:]
        out: list[EmailMessage] = []
        for num in numbers:
            self.requests += 1
            typ, parts = box.fetch(num.decode(), "(RFC822)")
            if typ != "OK":
                continue
            for part in parts or []:
                if isinstance(part, tuple) and len(part) == 2 and isinstance(part[1], bytes):
                    msg = email.message_from_bytes(part[1], policy=email.policy.default)
                    if isinstance(msg, EmailMessage):
                        out.append(msg)
        return out

    # -- plug-in API ---------------------------------------------------------------------------

    def discover(self, window: Window, profile: Profile, http: HttpClient) -> Iterator[Record]:
        box = self._open()
        if box is None:
            return
        try:
            messages = self._messages(box, window.start.astimezone(_dt.UTC).date())
        finally:
            with contextlib.suppress(OSError, imaplib.IMAP4.error):
                box.logout()
        if not messages:
            self.warn(
                f"scholar_imap: no mail from {self.config.sender_filter} in the window; "
                "check sources.scholar_imap.sender_filter"
            )
        for msg in messages:
            received = email.utils.parsedate_to_datetime(msg["Date"]) if msg["Date"] else None
            if received is not None:
                if received.tzinfo is None:
                    received = received.replace(tzinfo=_dt.UTC)
                if not window.contains(received):
                    continue
            html = alert_html(msg)
            items = parse_alert(html) if html else []
            if not items:
                self.warn(
                    "scholar_imap: an alert e-mail yielded no items (format change?); "
                    "it was kept unread in the mailbox"
                )
                continue
            for item in items:
                yield item_record(item, received.date() if received else None)

    def metadata(self, ident: Ident, http: HttpClient) -> Record | None:
        """Alerts cannot be queried by identifier (and Scholar is never contacted)."""
        return None

    def oa_pdf_candidates(self, record: Record, http: HttpClient) -> list[OaCandidate]:
        url = record.url or ""
        if url.lower().split("?", 1)[0].endswith(".pdf") or "arxiv.org/pdf/" in url:
            return [OaCandidate(url=url, source="scholar_imap")]
        return []

    def test(self, http: HttpClient) -> SourceTest:
        self.reset()
        box = self._open()
        key_present = not any("reference missing" in e for e in self.errors)
        if box is None:
            return SourceTest(
                ok=False,
                message="; ".join(self.errors) or "cannot open mailbox",
                source=self.name,
                key_present=key_present,
            )
        try:
            since = _dt.datetime.now(_dt.UTC).date() - _dt.timedelta(days=SEARCH_DAYS_FOR_TEST)
            found = len(self._messages(box, since))
        finally:
            with contextlib.suppress(OSError, imaplib.IMAP4.error):
                box.logout()
        message = "ok"
        if found == 0:
            message = (
                f"mailbox opened, but no mail from {self.config.sender_filter} in the last "
                f"{SEARCH_DAYS_FOR_TEST} days; check the real sender of your Scholar alerts and "
                "set sources.scholar_imap.sender_filter"
            )
        return SourceTest(
            ok=True,
            message=message,
            source=self.name,
            key_present=True,
            details={"messages": found, "warning": found == 0},
        )
