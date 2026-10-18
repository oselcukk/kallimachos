"""``fix-metadata``: Crossref/arXiv lookups, strong matches only, JSONL report.

Plan per item: DOI -> Crossref ``works/<doi>``; arXiv id (URL, ``extra`` or attachment filename)
-> arXiv API, and on to Crossref when the arXiv record carries a DOI; Crossref
``query.bibliographic`` + ``query.author`` (title similarity >= 0.9, year difference <= 2, same
first surname); without author only for titles of >= 6 words at >= 0.95. Only a strong match is
written. ``extra`` gets ``kallimachos: <source> <date>`` (and keeps ``eid:``); items that carry
that line are skipped unless ``--redo``. The report is JSONL (one ``json.dumps`` object per line,
so newlines, HTML and MathML inside titles cannot break it). Responses are cached in SQLite.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from kallimachos.interfaces import HttpClient
from kallimachos.process.cite import normalize_arxiv, normalize_doi, surname_key
from kallimachos.process.zotero import ZoteroClient, extra_value, item_data, set_extra_value
from kallimachos.records.normalize import match_key
from kallimachos.state import store

CROSSREF_WORKS = "https://api.crossref.org/works"
ARXIV_API = "http://export.arxiv.org/api/query"
TITLE_SIM_AUTHOR = 0.90
TITLE_SIM_NO_AUTHOR = 0.95
NO_AUTHOR_MIN_WORDS = 6
YEAR_TOLERANCE_AUTHOR = 2
YEAR_TOLERANCE_NO_AUTHOR = 1
MARK_KEY = "kallimachos"
SKIP_TYPES = frozenset({"attachment", "note", "annotation"})
_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV_NS = "{http://arxiv.org/schemas/atom}"


@dataclass
class Candidate:
    """Metadata found at a source, normalised to Zotero field names."""

    source: str
    title: str
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    creators: list[dict[str, str]] = field(default_factory=list)
    date: str | None = None

    @property
    def first_surname(self) -> str:
        if not self.creators:
            return ""
        c = self.creators[0]
        return surname_key(c.get("lastName") or c.get("name") or "")


class MetaCache:
    """``cache/zotero_meta.sqlite``: response bodies by request key."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, body TEXT NOT NULL,"
            " at TEXT NOT NULL)"
        )
        self.conn.commit()

    def get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT body FROM responses WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None

    def put(self, key: str, body: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO responses (key, body, at) VALUES (?, ?, ?)",
            (key, body, _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


Fetch = Callable[[str, Mapping[str, str] | None], str | None]


def make_fetch(http: HttpClient, cache: MetaCache) -> Fetch:
    def fetch(url: str, params: Mapping[str, str] | None) -> str | None:
        key = url + "?" + json.dumps(dict(sorted((params or {}).items())), ensure_ascii=False)
        cached = cache.get(key)
        if cached is not None:
            return cached
        resp = http.get(url, params=params, kind="metadata")
        if resp is None or resp.status != 200:
            return None
        body = resp.body.decode("utf-8", errors="replace")
        cache.put(key, body)
        return body

    return fetch


# --- source parsers -------------------------------------------------------------------------


def _crossref_candidate(msg: Mapping[str, Any]) -> Candidate | None:
    titles = msg.get("title") or []
    if not titles:
        return None
    issued = (msg.get("issued") or {}).get("date-parts") or [[None]]
    parts = issued[0] if issued else [None]
    year = int(parts[0]) if parts and parts[0] else None
    date = "-".join(f"{int(p):02d}" if i else str(p) for i, p in enumerate(parts) if p)
    creators = [
        {"creatorType": "author", "firstName": a.get("given", ""), "lastName": a.get("family", "")}
        if a.get("family")
        else {"creatorType": "author", "name": a.get("name", "")}
        for a in msg.get("author") or []
    ]
    venue = (msg.get("container-title") or [None])[0]
    return Candidate(
        source="crossref",
        title=" ".join(str(titles[0]).split()),
        year=year,
        venue=venue,
        doi=normalize_doi(str(msg.get("DOI") or "")),
        creators=creators,
        date=date or None,
    )


def crossref_by_doi(fetch: Fetch, doi: str) -> Candidate | None:
    body = fetch(f"{CROSSREF_WORKS}/{doi}", None)
    if not body:
        return None
    try:
        msg = json.loads(body).get("message") or {}
    except json.JSONDecodeError:
        return None
    return _crossref_candidate(msg)


def crossref_query(fetch: Fetch, title: str, author: str | None) -> list[Candidate]:
    params = {"query.bibliographic": title, "rows": "5"}
    if author:
        params["query.author"] = author
    body = fetch(CROSSREF_WORKS, params)
    if not body:
        return []
    try:
        items = (json.loads(body).get("message") or {}).get("items") or []
    except json.JSONDecodeError:
        return []
    out = [_crossref_candidate(m) for m in items]
    return [c for c in out if c is not None]


def arxiv_by_id(fetch: Fetch, arxiv_id: str) -> Candidate | None:
    body = fetch(ARXIV_API, {"id_list": arxiv_id})
    if not body:
        return None
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    entry = root.find(f"{_ATOM}entry")
    if entry is None:
        return None
    title = " ".join((entry.findtext(f"{_ATOM}title") or "").split())
    if not title:
        return None
    published = entry.findtext(f"{_ATOM}published") or ""
    year = int(published[:4]) if published[:4].isdigit() else None
    creators = []
    for a in entry.findall(f"{_ATOM}author"):
        name = " ".join((a.findtext(f"{_ATOM}name") or "").split())
        if name:
            parts = name.rsplit(" ", 1)
            creators.append(
                {"creatorType": "author", "firstName": parts[0], "lastName": parts[-1]}
                if len(parts) == 2
                else {"creatorType": "author", "name": name}
            )
    doi = normalize_doi(entry.findtext(f"{_ARXIV_NS}doi") or "")
    return Candidate(
        source="arxiv",
        title=title,
        year=year,
        venue=None,
        doi=doi,
        creators=creators,
        date=published[:10] or None,
    )


# --- matching -------------------------------------------------------------------------------


def title_similarity(a: str, b: str) -> float:
    ka, kb = match_key(a), match_key(b)
    if not ka or not kb:
        return 0.0
    return SequenceMatcher(None, ka, kb).ratio()


def item_year(data: Mapping[str, Any]) -> int | None:
    m = re.search(r"\b(1[89][0-9]{2}|20[0-9]{2})\b", str(data.get("date") or ""))
    return int(m.group(1)) if m else None


def item_first_surname(data: Mapping[str, Any]) -> str:
    creators = data.get("creators") or []
    if not creators:
        return ""
    c = creators[0]
    return surname_key(str(c.get("lastName") or c.get("name") or ""))


def arxiv_id_of(data: Mapping[str, Any], attachments: list[str]) -> str | None:
    for text in (data.get("url"), data.get("extra"), data.get("archiveID"), *attachments):
        if not text:
            continue
        found = normalize_arxiv(str(text).replace("-", "/") if "/" not in str(text) else str(text))
        found = found or normalize_arxiv(str(text))
        if found:
            return found
    return None


def strong_match(
    data: Mapping[str, Any], cands: list[Candidate], *, with_author: bool
) -> Candidate | None:
    title = str(data.get("title") or "")
    year = item_year(data)
    surname = item_first_surname(data)
    threshold = TITLE_SIM_AUTHOR if with_author else TITLE_SIM_NO_AUTHOR
    tolerance = YEAR_TOLERANCE_AUTHOR if with_author else YEAR_TOLERANCE_NO_AUTHOR
    for cand in cands:
        if title_similarity(title, cand.title) < threshold:
            continue
        if year is not None and cand.year is not None and abs(year - cand.year) > tolerance:
            continue
        if with_author and surname and cand.first_surname and surname != cand.first_surname:
            continue
        return cand
    return None


def plan(data: Mapping[str, Any], attachments: list[str], fetch: Fetch) -> Candidate | None:
    """Find the strongest metadata candidate for a Zotero item, or None."""
    doi = normalize_doi(str(data.get("DOI") or "")) or normalize_doi(str(data.get("extra") or ""))
    if doi:
        cand = crossref_by_doi(fetch, doi)
        if cand:
            return cand
    arxiv_id = arxiv_id_of(data, attachments)
    if arxiv_id:
        cand = arxiv_by_id(fetch, arxiv_id)
        if cand:
            if cand.doi:
                via = crossref_by_doi(fetch, cand.doi)
                if via:
                    via.source = "crossref-via-arxiv"
                    return via
            return cand
    title = " ".join(str(data.get("title") or "").split())
    if not title:
        return None
    creators = data.get("creators") or []
    first = str(creators[0].get("lastName") or creators[0].get("name") or "") if creators else ""
    if first:
        cand = strong_match(data, crossref_query(fetch, title, first), with_author=True)
        if cand:
            cand.source = "crossref-title-author"
            return cand
    if len(title.split()) >= NO_AUTHOR_MIN_WORDS:
        cand = strong_match(data, crossref_query(fetch, title, None), with_author=False)
        if cand:
            cand.source = "crossref-title"
            return cand
    return None


def apply_candidate(data: dict[str, Any], cand: Candidate, *, today: _dt.date) -> dict[str, Any]:
    """New payload: title/date/venue/DOI/creators from ``cand``; key, version and extra kept."""
    out = dict(data)
    out["title"] = cand.title
    if cand.date:
        out["date"] = cand.date
    if cand.venue and "publicationTitle" in out:
        out["publicationTitle"] = cand.venue
    if cand.doi and "DOI" in out:
        out["DOI"] = cand.doi
    if cand.creators:
        out["creators"] = cand.creators
    set_extra_value(out, MARK_KEY, f"{cand.source} {today.isoformat()}")
    return out


def fix_metadata(
    client: ZoteroClient,
    http: HttpClient,
    *,
    report_path: Path,
    cache_path: Path,
    dry_run: bool,
    redo: bool,
    today: _dt.date | None = None,
) -> list[dict[str, Any]]:
    """Run the plan over every regular item; write the JSONL report; return its rows."""
    today = today or _dt.date.today()
    cache = MetaCache(cache_path)
    try:
        fetch = make_fetch(http, cache)
        items = client.items()
        attachments: dict[str, list[str]] = {}
        for item in items:
            data = item_data(item)
            if data.get("itemType") == "attachment" and data.get("parentItem"):
                name = str(data.get("filename") or data.get("path") or data.get("title") or "")
                attachments.setdefault(str(data["parentItem"]), []).append(name)
        rows: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []
        for item in items:
            data = item_data(item)
            if data.get("itemType") in SKIP_TYPES:
                continue
            key = str(data.get("key") or item.get("key") or "")
            row: dict[str, Any] = {
                "key": key,
                "old_title": data.get("title"),
                "status": "unmatched",
            }
            if not redo and extra_value(data, MARK_KEY):
                row["status"] = "skipped"
                rows.append(row)
                continue
            cand = plan(data, attachments.get(key, []), fetch)
            if cand is None:
                rows.append(row)
                continue
            payload = apply_candidate(data, cand, today=today)
            payload.setdefault("key", item.get("key"))
            payload.setdefault("version", item.get("version"))
            row.update(
                status="dry_run" if dry_run else "fixed",
                source=cand.source,
                new_title=cand.title,
                new_venue=cand.venue,
                new_doi=cand.doi,
            )
            rows.append(row)
            updates.append(payload)
        client.update_items(updates, dry_run=dry_run)
        store.write_text_atomic(
            report_path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        )
        return rows
    finally:
        cache.close()
