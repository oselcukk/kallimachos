"""Journal proposals and ``full`` / ``topic_filtered`` mode.

Count ``primary_location.source`` over the seeds; a journal's yearly volume (OpenAlex
``meta.count`` for the previous year) above ``profile.journal_full_max_yearly_volume`` makes it
``topic_filtered``, otherwise ``full``. Journals whose volume is unknown stay ``topic_filtered``
(the safer mode). Nothing here is marked ``verified``; the user does that after review.
"""

from __future__ import annotations

import datetime as _dt
import json
from collections.abc import Mapping, Sequence

from kallimachos.config.schema import ProfileBuildConfig
from kallimachos.interfaces import HttpClient, Record, WorkKind
from kallimachos.profile.match import OPENALEX_API, short_openalex_id
from kallimachos.profile.schema import Journal
from kallimachos.records.normalize import match_key

_NO_JOURNAL_KINDS = frozenset({WorkKind.PREPRINT, WorkKind.BOOK, WorkKind.THESIS})
REPOSITORY_VENUE_KEYS = ("arxiv", "ssrn", "zenodo", "figshare", "researchgate", "hal", "biorxiv")


def propose_journals(
    seeds: Sequence[Record],
    http: HttpClient,
    cfg: ProfileBuildConfig,
    *,
    source_ids: Mapping[str, str] | None = None,
    year: int | None = None,
    min_seeds: int = 1,
    max_journals: int = 20,
    request_log: list[str] | None = None,
) -> list[Journal]:
    """Count venues over seeds; yearly volume > threshold -> topic_filtered.

    ``source_ids`` maps a venue name to its OpenAlex source id (``match.WorkExtra.source_id``);
    volumes are queried only for journals with a source id. ``year`` defaults to last year.
    """
    year = year or (_dt.date.today().year - 1)
    counts: dict[str, int] = {}
    names: dict[str, str] = {}
    issns: dict[str, list[str]] = {}
    for record in seeds:
        if not record.venue or record.kind in _NO_JOURNAL_KINDS:
            continue
        key = match_key(record.venue)
        if not key or any(rep in key for rep in REPOSITORY_VENUE_KEYS):
            continue
        counts[key] = counts.get(key, 0) + 1
        names.setdefault(key, record.venue)
        for issn in record.issn:
            issns.setdefault(key, [])
            if issn not in issns[key]:
                issns[key].append(issn)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    out: list[Journal] = []
    for key, count in ranked[:max_journals]:
        if count < min_seeds:
            continue
        name = names[key]
        source_id = (source_ids or {}).get(name) or (source_ids or {}).get(key)
        volume = (
            yearly_volume(source_id, year, http, request_log=request_log) if source_id else None
        )
        mode: str = "topic_filtered"
        if volume is not None and volume <= cfg.journal_full_max_yearly_volume:
            mode = "full"
        out.append(
            Journal(
                name=name,
                issn=issns.get(key, []),
                openalex_source_id=source_id,
                mode="full" if mode == "full" else "topic_filtered",
                yearly_volume=volume,
                verified=False,
            )
        )
    return out


def yearly_volume(
    openalex_source_id: str,
    year: int,
    http: HttpClient,
    *,
    request_log: list[str] | None = None,
) -> int | None:
    """``works?filter=primary_location.source.id:<id>,publication_year:<year>`` ->
    ``meta.count``."""
    short = short_openalex_id(openalex_source_id) or openalex_source_id
    params = {
        "filter": f"primary_location.source.id:{short},publication_year:{year}",
        "per-page": "1",
    }
    url = f"{OPENALEX_API}/works"
    if request_log is not None:
        request_log.append(f"{url}?filter={params['filter']}&per-page=1")
    resp = http.get(url, params=params, kind="list")
    if resp is None or resp.status != 200:
        return None
    try:
        data = json.loads(resp.text())
    except ValueError:
        return None
    count = (data.get("meta") or {}).get("count") if isinstance(data, dict) else None
    return int(count) if isinstance(count, int) else None
