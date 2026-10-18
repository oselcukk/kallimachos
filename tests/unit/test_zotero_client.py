"""Zotero client (paging, batched writes, no network), ``split-names``, ``reset-citekeys``."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from kallimachos.config.schema import ZoteroConfig
from kallimachos.interfaces import BudgetExhausted, ConfigError, HttpError, HttpResponse, UsageError
from kallimachos.process.zotero import PAGE_SIZE, ZoteroClient, extra_value, names, set_extra_value
from kallimachos.process.zotero import citekey as citekey_mod
from tests.unit.conftest_pdf import fake_store  # noqa: F401


class FakeHttp:
    """``HttpClient`` answering from a ``url -> body`` table (params appended as a query)."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[tuple[str, dict[str, str], dict[str, str]]] = []
        self._errors: list[HttpError] = []

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        tries: int = 3,
        timeout: float = 60.0,
        kind: str = "list",
    ) -> HttpResponse | None:
        params = dict(params or {})
        self.calls.append((url, params, dict(headers or {})))
        key = url + (
            "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else ""
        )
        if key not in self.routes:
            for route, value in self.routes.items():
                if callable(value) and key.startswith(route):
                    return value(url, params)
            return HttpResponse(url=url, status=404, headers={}, body=b"{}")
        value = self.routes[key]
        if callable(value):
            return value(url, params)
        body = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
        return HttpResponse(url=url, status=200, headers={}, body=body)

    @property
    def errors(self) -> list[HttpError]:
        return self._errors


class FakeWriter:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, dict[str, str], Any]] = []

    def request(
        self, method: str, url: str, *, headers: Mapping[str, str], body: bytes
    ) -> HttpResponse:
        payload = json.loads(body.decode("utf-8"))
        self.posts.append((method, url, dict(headers), payload))
        ok = {str(i): {"key": obj.get("key", "NEW00000")} for i, obj in enumerate(payload)}
        return HttpResponse(
            url=url,
            status=200,
            headers={},
            body=json.dumps({"successful": ok, "failed": {}}).encode("utf-8"),
        )


def item(key: str, version: int = 1, **data: Any) -> dict[str, Any]:
    d = {"key": key, "version": version, "itemType": "journalArticle", **data}
    return {"key": key, "version": version, "data": d}


CFG = ZoteroConfig(enabled=True, user_id=12345)
BASE = "https://api.zotero.org/users/12345"


def test_client_requires_user_id() -> None:
    with pytest.raises(ConfigError):
        ZoteroClient(ZoteroConfig(enabled=True), FakeHttp())


def test_items_pages_and_sends_key_header_only_in_headers() -> None:
    first = [item(f"K{i:03d}") for i in range(PAGE_SIZE)]
    second = [item("LAST")]
    http = FakeHttp(
        {
            f"{BASE}/items?format=json&limit=100&start=0": first,
            f"{BASE}/items?format=json&limit=100&start=100": second,
        }
    )
    client = ZoteroClient(CFG, http, api_key="secret-key-value", writer=FakeWriter())
    items = client.items()
    assert len(items) == PAGE_SIZE + 1 and items[-1]["key"] == "LAST"
    for url, params, headers in http.calls:
        assert "secret" not in url and "secret" not in json.dumps(params)
        assert headers["Zotero-API-Key"] == "secret-key-value"
        assert headers["Zotero-API-Version"] == "3"


def test_items_error_status_raises() -> None:
    http = FakeHttp(
        {
            f"{BASE}/items?format=json&limit=100&start=0": lambda u, p: HttpResponse(
                u, 403, {}, b"forbidden"
            )
        }
    )
    with pytest.raises(BudgetExhausted, match="403"):
        ZoteroClient(CFG, http, writer=FakeWriter()).items()


def test_update_items_batches_of_50_with_versions() -> None:
    writer = FakeWriter()
    client = ZoteroClient(CFG, FakeHttp(), api_key="k", writer=writer)
    items = [item(f"K{i:03d}", version=i) for i in range(120)]
    client.update_items(items)
    assert [len(p[3]) for p in writer.posts] == [50, 50, 20]
    assert writer.posts[0][0] == "POST" and writer.posts[0][1] == f"{BASE}/items"
    assert writer.posts[0][3][1] == {"key": "K001", "version": 1, "itemType": "journalArticle"}
    assert writer.posts[0][2]["Zotero-API-Key"] == "k"
    writer.posts.clear()
    client.update_items(items, dry_run=True)
    assert writer.posts == []
    with pytest.raises(ConfigError):
        client.update_items([{"title": "no key or version"}])


def test_create_note_and_dry_run() -> None:
    writer = FakeWriter()
    client = ZoteroClient(CFG, FakeHttp(), writer=writer)
    client.create_note("PARENT01", "<p>hi</p>")
    assert writer.posts[0][3] == [
        {"itemType": "note", "parentItem": "PARENT01", "note": "<p>hi</p>", "tags": []}
    ]
    assert client.create_note("PARENT01", "<p>x</p>", dry_run=True)["dry_run"] is True
    assert len(writer.posts) == 1


def test_extra_helpers() -> None:
    data: dict[str, Any] = {"extra": "eid: 0123456789ab\nOther: x"}
    assert extra_value(data, "eid") == "0123456789ab"
    assert extra_value(data, "missing") is None
    set_extra_value(data, "kallimachos", "crossref 2026-10-18")
    set_extra_value(data, "EID", "ffffffffffff")
    assert data["extra"].splitlines() == [
        "Other: x",
        "kallimachos: crossref 2026-10-18",
        "EID: ffffffffffff",
    ]


# --- split-names -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("blob", "expected"),
    [
        ("Example Author", ("Example", "Author")),
        ("Example Middle Author", ("Example Middle", "Author")),
        ("Ludwig van Beethoven", ("Ludwig", "van Beethoven")),
        ("Pierre de la Fontaine", ("Pierre", "de la Fontaine")),
        ("Author, Example", ("Example", "Author")),
        ("Plato", ("", "Plato")),
        ("  Example   Author  ", ("Example", "Author")),
    ],
)
def test_split_name(blob: str, expected: tuple[str, str]) -> None:
    assert names.split_name(blob) == expected


def test_needs_split_and_split_creators() -> None:
    assert names.needs_split(
        {"creatorType": "author", "firstName": "", "lastName": "Example Author"}
    )
    assert not names.needs_split(
        {"creatorType": "author", "firstName": "E.", "lastName": "Example Author"}
    )
    assert not names.needs_split({"creatorType": "author", "name": "Example Author"})
    assert not names.needs_split({"firstName": "", "lastName": "Fictional University Press"})
    assert not names.needs_split({"firstName": "", "lastName": "One Two Three Four Five"})
    assert not names.needs_split({"firstName": "", "lastName": "Group 42"})
    data = {
        "creators": [
            {"creatorType": "author", "firstName": "", "lastName": "Example Author"},
            {"creatorType": "author", "firstName": "", "lastName": "Single"},
        ]
    }
    assert names.split_creators(data) is True
    assert data["creators"][0] == {
        "creatorType": "author",
        "firstName": "Example",
        "lastName": "Author",
    }
    assert data["creators"][1]["lastName"] == "Single"
    assert names.split_creators(data) is False  # idempotent


# --- reset-citekeys ----------------------------------------------------------------------------


def test_reset_citekeys_selection_and_idempotence() -> None:
    writer = FakeWriter()
    http = FakeHttp(
        {
            f"{BASE}/items?format=json&limit=100&start=0": [
                item("AAAA0001", citationKey="author2019a", title="Fictional lattices"),
                item("BBBB0002", citationKey="", title="Fictional lattices II"),
                item("CCCC0003", citationKey="other2001", title="Unrelated"),
                {
                    "key": "NOTE0001",
                    "version": 1,
                    "data": {
                        "key": "NOTE0001",
                        "version": 1,
                        "itemType": "note",
                        "citationKey": "x",
                    },
                },
            ]
        }
    )
    client = ZoteroClient(CFG, http, writer=writer)
    with pytest.raises(UsageError):
        citekey_mod.reset_citekeys(client, keys=None, item_filter=None, dry_run=False)
    with pytest.raises(UsageError):
        citekey_mod.reset_citekeys(client, keys=None, item_filter="nonsense", dry_run=False)
    n = citekey_mod.reset_citekeys(client, keys=None, item_filter="title=lattices", dry_run=False)
    assert n == 1  # BBBB0002 already empty, CCCC0003 not selected, the note skipped
    assert writer.posts[0][3] == [
        {
            "key": "AAAA0001",
            "version": 1,
            "itemType": "journalArticle",
            "citationKey": "",
            "title": "Fictional lattices",
        }
    ]
    assert (
        citekey_mod.reset_citekeys(client, keys=["CCCC0003"], item_filter=None, dry_run=True) == 1
    )
    assert len(writer.posts) == 1  # dry run posted nothing
