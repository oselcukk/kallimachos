"""Channel / folder assignment.

trigger ``channel=`` > ``scoring.topic.score()`` main channel > ``_Unsorted``; always_show authors
go to ``persons/<folder>/``. Folders come only from the profile.

Folder precedence: trigger ``person=`` > trigger ``channel=`` > always_show author > scored
main channel > ``_Unsorted``. The channel id is recorded whenever one is known.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from kallimachos.archive.naming import person_folder_name
from kallimachos.config.schema import Config, ScoringConfig
from kallimachos.interfaces import ArchiveTrigger, Record, Weights
from kallimachos.profile.schema import AlwaysShowAuthor, Profile
from kallimachos.records.normalize import match_key

UNSORTED = "_Unsorted"
PERSONS = "persons"
_SAFE_FOLDER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,80}$")

MainChannel = Callable[[Record, Profile, Weights, ScoringConfig], str | None]


def scored_main_channel(
    record: Record, profile: Profile, weights: Weights, cfg: ScoringConfig
) -> str | None:
    """Main channel from ``scoring.topic.score`` (the one classifier)."""
    from kallimachos.scoring import topic

    return topic.score(record, profile, weights, cfg).main_channel


def always_show_match(record: Record, profile: Profile) -> AlwaysShowAuthor | None:
    """First always_show author of the record, by ORCID / OpenAlex id / name or alias."""
    for author in record.authors:
        for person in profile.authors.always_show:
            if person.orcid and author.orcid and person.orcid == author.orcid:
                return person
            if (
                person.openalex_id
                and author.openalex_id
                and person.openalex_id == author.openalex_id
            ):
                return person
            names = {match_key(person.name), *(match_key(a) for a in person.aliases)}
            if match_key(author.name) in names:
                return person
    return None


def folder_for(
    record: Record,
    trigger: ArchiveTrigger,
    profile: Profile,
    weights: Weights,
    config: Config,
    *,
    main_channel: MainChannel = scored_main_channel,
) -> tuple[str | None, str]:
    """Return (channel id or None, relative folder)."""
    hinted = profile.channel_by_id(trigger.channel_hint) if trigger.channel_hint else None
    channel_id = hinted.id if hinted else None
    if channel_id is None:
        scored = main_channel(record, profile, weights, config.scoring)
        if scored and profile.channel_by_id(scored):
            channel_id = scored
    if trigger.person_folder and _SAFE_FOLDER.match(trigger.person_folder):
        return channel_id, f"{PERSONS}/{trigger.person_folder}"
    if hinted is not None:
        return channel_id, hinted.folder
    person = always_show_match(record, profile)
    if person is not None:
        name = person.folder or person_folder_name(person.name)
        if _SAFE_FOLDER.match(name):
            return channel_id, f"{PERSONS}/{name}"
    channel = profile.channel_by_id(channel_id) if channel_id else None
    if channel is not None:
        return channel_id, channel.folder
    return None, UNSORTED
