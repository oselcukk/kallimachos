"""Shared helpers for sources.

Every source talks to the network only through the :class:`~kallimachos.interfaces.HttpClient`
it is given. Secrets are resolved from ``*_ref`` references at use time and are sent only where
the provider documents them (a header; for PubMed the ``api_key`` query parameter, passed in
``params`` so the HTTP layer can keep it out of cache keys and logs). They never appear in a URL
string, a record, an error message or a warning.
"""

from __future__ import annotations

import datetime as _dt
import email.utils
import html
import json
import re
import time
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

from kallimachos.interfaces import (
    ConfigError,
    HttpClient,
    HttpError,
    HttpResponse,
    Ident,
    IdentKind,
    OaCandidate,
    Record,
    Source,
    SourceStats,
    SourceTest,
    Window,
)
from kallimachos.profile.schema import Profile
from kallimachos.records.normalize import collapse_whitespace, nfc

SecretResolver = Callable[[str], str]
"""Turns a ``keyring:``/``env:`` reference into the secret (``kallimachos.secrets.resolve_ref``)."""

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.IGNORECASE)
ARXIV_URL_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf)/([a-z\-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})(v\d+)?", re.IGNORECASE
)
ARXIV_DOI_PREFIX = "10.48550/arxiv."
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_TAG_RE = re.compile(
    r"</?(?:[a-z]+:)?(?:p|br|div|li|ul|ol|h[1-6]|title|sec|table|tr|td)\b[^>]*>", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# text and dates
# ---------------------------------------------------------------------------


def clean_text(value: str | None) -> str:
    """NFC, drop markup (block tags become spaces, inline tags vanish), unescape entities,
    collapse whitespace."""
    if not value:
        return ""
    text = html.unescape(_TAG_RE.sub("", _BLOCK_TAG_RE.sub(" ", value)))
    return collapse_whitespace(nfc(text))


def element_text(elem: ET.Element | None) -> str:
    """All text inside ``elem`` (mixed content such as ``<i>`` or JATS children included)."""
    if elem is None:
        return ""
    return clean_text("".join(elem.itertext()))


def parse_iso_date(value: str | None) -> _dt.date | None:
    """Lenient ISO date parser shared by sources (``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD``, datetimes).

    ``YYYY`` and ``YYYY-MM`` become the first day of the period; unparseable input is ``None``.
    """
    if not value:
        return None
    m = re.match(r"^(\d{4})(?:[-/](\d{1,2})(?:[-/](\d{1,2}))?)?(?:$|[T\s])", value.strip())
    if not m:
        return None
    try:
        return _dt.date(int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1))
    except ValueError:
        return None


def parse_datetime(value: str | None) -> _dt.datetime | None:
    """ISO 8601 or RFC 822 (RSS ``pubDate``) to an aware UTC datetime. Date-only values become
    midnight UTC."""
    if not value:
        return None
    text = value.strip()
    parsed: _dt.datetime | None
    try:
        parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None:
        try:
            parsed = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            parsed = None
    if parsed is None:
        day = parse_iso_date(text)
        if day is None:
            return None
        parsed = _dt.datetime(day.year, day.month, day.day)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.UTC)
    return parsed.astimezone(_dt.UTC)


def date_parts(obj: Mapping[str, Any] | None) -> _dt.date | None:
    """Crossref ``{"date-parts": [[2026, 10, 2]]}`` to a date (missing month/day = 1)."""
    if not obj:
        return None
    parts = (obj.get("date-parts") or [[None]])[0]
    if not parts or parts[0] is None:
        return None
    try:
        nums = [int(p) for p in parts[:3]]
        while len(nums) < 3:
            nums.append(1)
        return _dt.date(nums[0], nums[1], nums[2])
    except (TypeError, ValueError):
        return None


def day_in_window(day: _dt.date | None, window: Window) -> bool:
    """A date-only value is inside the window when its UTC day overlaps ``[start, end)``."""
    if day is None:
        return False
    start = _dt.datetime(day.year, day.month, day.day, tzinfo=_dt.UTC)
    return start < window.end and start + _dt.timedelta(days=1) > window.start


# ---------------------------------------------------------------------------
# identifiers
# ---------------------------------------------------------------------------


def normalize_doi(value: str | None) -> str | None:
    """Lower-case bare DOI (``https://doi.org/`` and ``doi:`` prefixes removed)."""
    if not value:
        return None
    text = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", "", value.strip(), flags=re.IGNORECASE)
    m = DOI_RE.search(text)
    if not m:
        return None
    return m.group(1).rstrip(".,;)").lower()


def find_doi(*texts: str | None) -> str | None:
    for text in texts:
        doi = normalize_doi(text)
        if doi:
            return doi
    return None


def find_arxiv(*texts: str | None) -> tuple[str, str | None] | None:
    """``(id, "vN" | None)`` from an arXiv URL or a ``10.48550/arXiv.<id>`` DOI."""
    for text in texts:
        if not text:
            continue
        m = ARXIV_URL_RE.search(text)
        if m:
            return m.group(1), m.group(2)
        low = text.lower()
        if ARXIV_DOI_PREFIX in low:
            ident = low.split(ARXIV_DOI_PREFIX, 1)[1].strip()
            if ident:
                return strip_version(ident)[0], None
    return None


def strip_version(arxiv_id: str) -> tuple[str, str | None]:
    """``"2610.00001v2"`` -> ``("2610.00001", "v2")``."""
    m = re.match(r"^(.*?)(v\d+)$", arxiv_id)
    if m:
        return m.group(1), m.group(2)
    return arxiv_id, None


def doi_of(ident: Ident) -> str | None:
    return normalize_doi(ident.value) if ident.kind is IdentKind.DOI else None


# ---------------------------------------------------------------------------
# profile helpers
# ---------------------------------------------------------------------------


def core_terms(profile: Profile) -> dict[str, list[str]]:
    """Channel id -> core keywords (deduplicated case-insensitively, original spelling)."""
    out: dict[str, list[str]] = {}
    for ch in profile.channels:
        terms: list[str] = []
        lowered: set[str] = set()
        for term in ch.keywords.core:
            t = collapse_whitespace(term)
            if t and t.lower() not in lowered:
                lowered.add(t.lower())
                terms.append(t)
        if terms:
            out[ch.id] = terms
    return out


def chunked(items: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def parse_xml(body: bytes) -> ET.Element | None:
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        return None


def parse_json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# HTTP wrapper and base class
# ---------------------------------------------------------------------------


@dataclass
class Fetch:
    """One ``HttpClient.get`` call: the response (``None`` on failure) and the errors it added."""

    response: HttpResponse | None
    errors: list[HttpError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.response is not None and 200 <= self.response.status < 300

    @property
    def status(self) -> int | None:
        if self.response is not None:
            return self.response.status
        for err in reversed(self.errors):
            if err.status is not None:
                return err.status
        return None

    @property
    def body(self) -> bytes:
        return self.response.body if self.response is not None else b""


class BaseSource:
    """Default implementation returning nothing; concrete sources override what they support.

    Adds request counting, secret resolution and per-run ``errors``/``warnings`` lists that
    :func:`run_source` copies into :class:`~kallimachos.interfaces.SourceStats`.
    """

    name: str = ""
    needs_key: bool = False
    default_interval_s: float = 0.5
    hosts: tuple[str, ...] = ()
    """Hosts this source contacts; used for per-host intervals (``registry.host_intervals``)."""

    def __init__(
        self, config: object = None, *, resolve_secret: SecretResolver | None = None
    ) -> None:
        self._resolve_secret = resolve_secret
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.requests = 0
        self.silenced: list[HttpError] = []

    # -- plug-in API -------------------------------------------------------------------------

    def discover(self, window: Window, profile: Profile, http: HttpClient) -> Iterator[Record]:
        return iter(())

    def metadata(self, ident: Ident, http: HttpClient) -> Record | None:
        return None

    def oa_pdf_candidates(self, record: Record, http: HttpClient) -> list[OaCandidate]:
        return []

    def test(self, http: HttpClient) -> SourceTest:
        return SourceTest(ok=False, message="test not supported", source=self.name)

    def key_refs(self) -> dict[str, str | None]:
        """Label -> secret reference this source may use (``sources list`` shows present/absent
        only)."""
        return {}

    def reset(self) -> None:
        """Clear per-run counters (called by :func:`run_source`)."""
        self.errors = []
        self.warnings = []
        self.requests = 0
        self.silenced = []

    # -- helpers -----------------------------------------------------------------------------

    def secret(self, ref: str | None, label: str) -> str | None:
        """Resolve ``ref``. A missing secret is a warning (never the value); the source carries on
        without it."""
        if not ref:
            return None
        resolver = self._resolve_secret
        if resolver is None:
            from kallimachos.secrets import resolve_ref

            resolver = resolve_ref
        try:
            value = resolver(ref)
        except ConfigError:
            self.warn(f"{self.name}: {label} reference is set but the secret was not found")
            return None
        return value or None

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def error(self, message: str) -> None:
        self.errors.append(message)

    def silence(self, result: Fetch) -> None:
        """Mark the HTTP errors of ``result`` as expected (e.g. zbMATH ``noRecordsMatch``): they
        stay in the HTTP log but are not reported as source errors."""
        self.silenced.extend(result.errors)

    def fetch(
        self,
        http: HttpClient,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        kind: str = "list",
        timeout: float = 60.0,
        tries: int = 3,
        accept_status: Collection[int] = (),
    ) -> Fetch:
        """One ``HttpClient.get``. ``accept_status`` lists error statuses whose body the source
        wants to read (the client returns them as responses instead of ``None`` + error)."""
        before = len(http.errors)
        self.requests += 1
        extra: dict[str, Any] = {"accept_status": tuple(accept_status)} if accept_status else {}
        response = http.get(
            url,
            params=dict(params) if params else None,
            headers=dict(headers) if headers else None,
            tries=tries,
            timeout=timeout,
            kind=kind,
            **extra,
        )
        return Fetch(response=response, errors=list(http.errors[before:]))

    def timed_test(self, run: Callable[[], Fetch], check: Callable[[Fetch], str]) -> SourceTest:
        """Run one request; ``check`` returns ``""`` when the body looks right, else a reason."""
        started = time.monotonic()
        result = run()
        latency = round(time.monotonic() - started, 3)
        if not result.ok:
            reason = result.errors[-1].message if result.errors else "no response"
            return SourceTest(
                ok=False,
                message=f"request failed: {reason}",
                latency_s=latency,
                source=self.name,
                http_status=result.status,
            )
        problem = check(result)
        return SourceTest(
            ok=not problem,
            message=problem or "ok",
            latency_s=latency,
            source=self.name,
            http_status=result.status,
        )


# ---------------------------------------------------------------------------
# running one source inside a scan
# ---------------------------------------------------------------------------


@dataclass
class SourceRun:
    """What one ``discover`` call produced. A failure never propagates."""

    records: list[Record]
    stats: SourceStats
    warnings: list[str] = field(default_factory=list)


def run_source(source: Source, window: Window, profile: Profile, http: HttpClient) -> SourceRun:
    """Run ``source.discover`` to completion. Exceptions and HTTP failures end up in
    ``stats.errors``; the caller's run continues with the other sources."""
    before = len(http.errors)
    if isinstance(source, BaseSource):
        source.reset()
    records: list[Record] = []
    crash: list[str] = []
    try:
        records.extend(source.discover(window, profile, http))
    except Exception as exc:  # a broken source must not stop the run
        crash.append(f"{source.name}: {type(exc).__name__}: {exc}"[:300])
    silenced = source.silenced if isinstance(source, BaseSource) else []
    errors = [e.message for e in http.errors[before:] if not any(e is s for s in silenced)]
    warnings: list[str] = []
    requests = 0
    if isinstance(source, BaseSource):
        errors.extend(source.errors)
        warnings = list(source.warnings)
        requests = source.requests
    errors.extend(crash)
    stats = SourceStats(count=len(records), requests=requests, errors=errors)
    cost = getattr(source, "cost_usd", None)
    if isinstance(cost, float):
        stats.budget = round(cost, 6)
    return SourceRun(records=records, stats=stats, warnings=warnings)
