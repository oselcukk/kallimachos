"""Zotero Web API v3 client and sub-commands.

Reads go through the shared :class:`~kallimachos.interfaces.HttpClient` (``GET``). Writes
(``POST``) go through the :class:`Writer` protocol whose default, :class:`HttpWriter`, is the
shared ``Http.request`` (one place for pacing and secret headers; ``OFFLINE=1`` means no
transport, hence no write). Tests inject fakes for both.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol, cast

from kallimachos.config.schema import ZoteroConfig
from kallimachos.interfaces import BudgetExhausted, ConfigError, HttpClient, HttpResponse

API_BASE = "https://api.zotero.org"
API_VERSION = "3"
PAGE_SIZE = 100
WRITE_BATCH = 50
NOTE_MARK = "Kallimachos note"
"""Marker inside the child note that ``link`` creates; the note is found again by this text."""


class Writer(Protocol):
    """Minimal write transport (``HttpClient`` has no POST)."""

    def request(
        self, method: str, url: str, *, headers: Mapping[str, str], body: bytes
    ) -> HttpResponse: ...


class HttpWriter:
    """Default :class:`Writer`: the shared :class:`kallimachos.http.client.Http` ``request`` method
    (pacing, secret-header rules and ``OFFLINE`` handling live there, once). Any status is handed
    back as a response so the client can report the Zotero error body."""

    def __init__(self, http: HttpClient) -> None:
        request = getattr(http, "request", None)
        if not callable(request):
            raise ConfigError("Zotero writes need the shared HTTP client (http.client.Http)")
        self._request = request

    def request(
        self, method: str, url: str, *, headers: Mapping[str, str], body: bytes
    ) -> HttpResponse:
        resp = self._request(
            method,
            url,
            headers=headers,
            body=body,
            tries=1,
            kind="write",
            accept_status=range(400, 600),
        )
        if resp is None:
            raise BudgetExhausted("Zotero write: no response (offline or network failure)")
        return cast(HttpResponse, resp)


class ZoteroClient:
    def __init__(
        self,
        cfg: ZoteroConfig,
        http: HttpClient,
        *,
        api_key: str | None = None,
        writer: Writer | None = None,
    ) -> None:
        self.cfg = cfg
        self.http = http
        self.api_key = api_key
        self.writer: Writer = writer or HttpWriter(http)
        if cfg.user_id in (None, ""):
            raise ConfigError("process.zotero.user_id is not set")
        prefix = "users" if cfg.library_type == "user" else "groups"
        self.base = f"{API_BASE}/{prefix}/{cfg.user_id}"

    # --- helpers -----------------------------------------------------------------------------

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {"Zotero-API-Version": API_VERSION}
        if self.api_key:
            headers["Zotero-API-Key"] = self.api_key
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _json(resp: HttpResponse | None, *, what: str) -> Any:
        if resp is None:
            raise BudgetExhausted(f"Zotero {what}: no response")
        if resp.status >= 400:
            snippet = resp.body[:200].decode("utf-8", errors="replace")
            raise BudgetExhausted(f"Zotero {what}: HTTP {resp.status} {snippet}")
        return json.loads(resp.body.decode("utf-8")) if resp.body else None

    # --- reads -------------------------------------------------------------------------------

    def items(self, *, since: int | None = None) -> list[dict[str, Any]]:
        """Every top-level item of the library (``GET /items?format=json``), paged by 100."""
        out: list[dict[str, Any]] = []
        start = 0
        while True:
            params = {"format": "json", "limit": str(PAGE_SIZE), "start": str(start)}
            if since is not None:
                params["since"] = str(since)
            resp = self.http.get(
                f"{self.base}/items", params=params, headers=self._headers(), kind="list"
            )
            page = self._json(resp, what="items")
            if not isinstance(page, list):
                raise BudgetExhausted("Zotero items: unexpected payload")
            out.extend(page)
            if len(page) < PAGE_SIZE:
                break
            start += PAGE_SIZE
            headers = resp.headers if resp is not None else {}
            total = headers.get("total-results") or headers.get("Total-Results")
            if total is not None and start >= int(total):
                break
        return out

    # --- writes ------------------------------------------------------------------------------

    def _post(self, payload: list[dict[str, Any]]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        resp = self.writer.request(
            "POST",
            f"{self.base}/items",
            headers=self._headers({"Content-Type": "application/json"}),
            body=body,
        )
        result = self._json(resp, what="write")
        return result if isinstance(result, dict) else {}

    def update_items(
        self, items: list[dict[str, Any]], *, dry_run: bool = False
    ) -> list[dict[str, Any]]:
        """POST in batches of 50; every object carries its ``key`` and ``version`` so the server
        rejects stale writes (412). Returns the per-batch response dicts (empty when ``dry_run``).
        """
        results: list[dict[str, Any]] = []
        payload = [_data_of(item) for item in items]
        for i in range(0, len(payload), WRITE_BATCH):
            batch = payload[i : i + WRITE_BATCH]
            for obj in batch:
                if "key" not in obj or "version" not in obj:
                    raise ConfigError("Zotero update: every item needs key and version")
            if dry_run:
                continue
            results.append(self._post(batch))
        return results

    def create_note(self, parent_key: str, html: str, *, dry_run: bool = False) -> dict[str, Any]:
        note = {"itemType": "note", "parentItem": parent_key, "note": html, "tags": []}
        if dry_run:
            return {"dry_run": True, "note": note}
        return self._post([note])


def _data_of(item: Mapping[str, Any]) -> dict[str, Any]:
    """A Web API item (``{key, version, data: {...}}``) or a bare ``data`` dict -> write payload."""
    data = item.get("data")
    if isinstance(data, dict):
        out = dict(data)
        out.setdefault("key", item.get("key"))
        out.setdefault("version", item.get("version"))
        return out
    return dict(item)


def item_data(item: Mapping[str, Any]) -> dict[str, Any]:
    data = item.get("data")
    return data if isinstance(data, dict) else dict(item)


def extra_lines(data: Mapping[str, Any]) -> list[str]:
    extra = data.get("extra")
    return str(extra).splitlines() if extra else []


def extra_value(data: Mapping[str, Any], key: str) -> str | None:
    """Value of ``<key>: <value>`` in the item's ``extra`` field (first match)."""
    prefix = f"{key}:"
    for line in extra_lines(data):
        if line.lower().startswith(prefix.lower()):
            return line[len(prefix) :].strip()
    return None


def set_extra_value(data: dict[str, Any], key: str, value: str) -> None:
    """Replace or append ``<key>: <value>`` in ``extra`` (other lines untouched)."""
    prefix = f"{key}:"
    lines = [ln for ln in extra_lines(data) if not ln.lower().startswith(prefix.lower())]
    lines.append(f"{key}: {value}")
    data["extra"] = "\n".join(lines)
