"""Profile from question-and-answer input.

``answers.profile.qa`` is free-form by design: the agent tabulates what the user
said. Accepted shapes:

* ``channels``: list of mappings ``{id?, name, core|terms, broad?, priority?, folder?, exclude?,
  penalty?, arxiv?, msc?, noise?}`` or strings ``"Name: term1; term2; term3"``.
* ``authors``: strings or ``{name, orcid?, openalex_id?, always_show?: bool, aliases?, channels?}``.
* ``journals``: strings or ``{name, issn?, mode?, channel?}``.
* ``arxiv_categories`` / ``codes`` (MSC or other codes): lists; applied to channels that have none.
* ``exclude``: excluded topics (profile-wide).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from kallimachos.interfaces import ConfigError
from kallimachos.profile.schema import (
    AlwaysShowAuthor,
    Authors,
    Channel,
    CodeSet,
    FollowAuthor,
    Journal,
    Keywords,
    NegativeTerms,
    Profile,
)
from kallimachos.records.normalize import collapse_whitespace, match_key

CHANNEL_IDS = "ABCDEFGHIJKL"
_ISSN_RE = re.compile(r"\b\d{4}-\d{3}[\dXx]\b")
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,15}$")


def profile_from_answers(
    qa: dict[str, Any], *, language_hint: str, min_core_terms: int = 5
) -> tuple[Profile, list[str]]:
    """Build a draft profile from ``answers.profile.qa``; warn when a channel has fewer than
    ``profile.min_core_terms_warning`` core terms."""
    warnings: list[str] = []
    field = collapse_whitespace(str(qa.get("field") or ""))
    channels = [
        _channel(index, raw, used_ids=set())
        for index, raw in enumerate(_as_list(qa.get("channels")))
    ]
    _fix_duplicate_ids(channels)
    if not channels and field:
        channels.append(
            Channel(id="A", name=field[:80], folder=f"A-{slug(field)}", keywords=Keywords())
        )
    arxiv_categories = _strings(qa.get("arxiv_categories"))
    codes = _strings(qa.get("codes"))
    for ch in channels:
        if arxiv_categories and not ch.arxiv.primary:
            ch.arxiv = CodeSet(primary=list(arxiv_categories))
        if codes and not ch.msc.primary:
            ch.msc = CodeSet(primary=list(codes))
    _attach_journals(channels, _as_list(qa.get("journals")))
    authors = _authors(_as_list(qa.get("authors")), channels)
    excluded = [collapse_whitespace(str(e)) for e in _as_list(qa.get("exclude")) if str(e).strip()]
    for ch in channels:
        if len(ch.keywords.core) < min_core_terms:
            warnings.append(
                f"channel {ch.id} ({ch.name}) has {len(ch.keywords.core)} core terms; "
                f"at least {min_core_terms} are recommended"
            )
    if not channels:
        warnings.append("no channel was given; add at least one before approving")
    profile = Profile(
        status="draft",
        language_hint=language_hint,
        field=field or (match_key(channels[0].name).replace(" ", "-") if channels else ""),
        channels=channels,
        authors=authors,
        excluded_topics=excluded,
        notes="Draft built from the install wizard answers; review and approve.",
    )
    return profile, warnings


def slug(text: str, limit: int = 60) -> str:
    words = [w.capitalize() for w in match_key(text).split()]
    return ("-".join(words)[:limit].strip("-.")) or "Channel"


def split_terms(value: Any) -> list[str]:
    """``"a; b, c"`` or a list -> lower-cased, de-duplicated phrases."""
    if value is None:
        return []
    items: Iterable[Any] = value if isinstance(value, list) else re.split(r"[;,\n]", str(value))
    out: list[str] = []
    for item in items:
        term = collapse_whitespace(str(item)).lower()
        if term and term not in out:
            out.append(term)
    return out


def _strings(value: Any) -> list[str]:
    return [str(c).strip() for c in _as_list(value) if str(c).strip()]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _channel(index: int, raw: Any, *, used_ids: set[str]) -> Channel:
    if index >= len(CHANNEL_IDS):
        raise ConfigError(f"too many channels (max {len(CHANNEL_IDS)})")
    default_id = CHANNEL_IDS[index]
    if isinstance(raw, str):
        name, _, terms = raw.partition(":")
        name = collapse_whitespace(name) or f"Channel {default_id}"
        data: dict[str, Any] = {"name": name, "core": terms}
    elif isinstance(raw, dict):
        data = dict(raw)
    else:
        raise ConfigError(f"profile.qa.channels[{index}] must be a mapping or a string")
    name = collapse_whitespace(str(data.get("name") or f"Channel {default_id}"))[:80]
    cid = collapse_whitespace(str(data.get("id") or default_id))
    if not _ID_RE.match(cid):
        raise ConfigError(f"profile.qa.channels[{index}].id {cid!r} is not a valid channel id")
    used_ids.add(cid)
    folder = collapse_whitespace(str(data.get("folder") or f"{cid}-{slug(name)}"))
    priority_raw = data.get("priority", 1)
    try:
        priority = int(priority_raw)
    except (TypeError, ValueError):
        raise ConfigError(f"profile.qa.channels[{index}].priority must be 1..3") from None
    if priority not in (1, 2, 3):
        raise ConfigError(f"profile.qa.channels[{index}].priority must be 1..3")
    core = split_terms(data.get("core") or data.get("terms") or data.get("keywords"))
    broad = split_terms(data.get("broad"))
    return Channel(
        id=cid,
        name=name,
        folder=folder,
        priority=priority,
        keywords=Keywords(
            core=core, broad=broad, spelling_note=str(data.get("spelling_note") or "")
        ),
        negative=NegativeTerms(
            exclude=split_terms(data.get("exclude")), penalty=split_terms(data.get("penalty"))
        ),
        msc=CodeSet(
            primary=_strings(data.get("msc")), secondary=_strings(data.get("msc_secondary"))
        ),
        arxiv=CodeSet(
            primary=_strings(data.get("arxiv")), secondary=_strings(data.get("arxiv_secondary"))
        ),
        noise_terms=split_terms(data.get("noise")),
    )


def _fix_duplicate_ids(channels: list[Channel]) -> None:
    seen: set[str] = set()
    for ch in channels:
        if ch.id in seen:
            raise ConfigError(f"duplicate channel id {ch.id!r} in profile.qa.channels")
        seen.add(ch.id)


def _attach_journals(channels: list[Channel], raw_journals: list[Any]) -> None:
    for raw in raw_journals:
        if isinstance(raw, str):
            issn = _ISSN_RE.findall(raw)
            name = collapse_whitespace(_ISSN_RE.sub("", raw).strip(" ,;:()[]-"))
            data: dict[str, Any] = {"name": name, "issn": issn}
        elif isinstance(raw, dict):
            data = dict(raw)
        else:
            continue
        name = collapse_whitespace(str(data.get("name") or ""))
        if not name:
            continue
        issn_raw = data.get("issn")
        issn = [str(i).upper() for i in _as_list(issn_raw) if _ISSN_RE.match(str(i))]
        mode = str(data.get("mode") or "topic_filtered")
        if mode not in ("full", "topic_filtered"):
            raise ConfigError(f"journal {name!r}: mode must be full or topic_filtered")
        journal = Journal(
            name=name,
            issn=issn,
            openalex_source_id=(
                str(data["openalex_source_id"]) if data.get("openalex_source_id") else None
            ),
            mode="full" if mode == "full" else "topic_filtered",
            yearly_volume=int(data["yearly_volume"]) if data.get("yearly_volume") else None,
        )
        target = _channel_for(channels, data.get("channel"))
        if target is not None:
            target.journals.append(journal)


def _channel_for(channels: list[Channel], hint: Any) -> Channel | None:
    if not channels:
        return None
    if hint is None:
        return channels[0]
    text = str(hint)
    for ch in channels:
        if ch.id == text or match_key(ch.name) == match_key(text):
            return ch
    return channels[0]


def _authors(raw_authors: list[Any], channels: list[Channel]) -> Authors:
    authors = Authors()
    for raw in raw_authors:
        if isinstance(raw, str):
            data: dict[str, Any] = {"name": raw}
        elif isinstance(raw, dict):
            data = dict(raw)
        else:
            continue
        name = collapse_whitespace(str(data.get("name") or ""))
        if not name:
            continue
        orcid = str(data["orcid"]).rsplit("/", 1)[-1] if data.get("orcid") else None
        oa_id = str(data["openalex_id"]).rsplit("/", 1)[-1] if data.get("openalex_id") else None
        if data.get("always_show"):
            authors.always_show.append(
                AlwaysShowAuthor(
                    name=name,
                    orcid=orcid,
                    openalex_id=oa_id,
                    aliases=[collapse_whitespace(str(a)) for a in _as_list(data.get("aliases"))],
                )
            )
            continue
        channel_ids = [
            ch.id
            for hint in _as_list(data.get("channels") or data.get("channel"))
            if (ch := _channel_for(channels, hint)) is not None
        ]
        authors.follow.append(
            FollowAuthor(
                name=name,
                orcid=orcid,
                openalex_id=oa_id,
                status="verified" if (orcid or oa_id) and data.get("verified") else "candidate",
                channels=list(dict.fromkeys(channel_ids)),
            )
        )
    return authors
