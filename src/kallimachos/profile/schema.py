"""``profile.yaml`` shape.

The profile is the single source for channel -> folder, journal lists, author lists and
category mappings. Dead keys are rejected by :func:`kallimachos.interfaces.build`.
Profile *building* (wizard, importers, matching) lives in the other ``profile`` modules.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
from dataclasses import dataclass
from typing import Any, Literal

from kallimachos.interfaces import SCHEMA_REGISTRY, RuleStage

PROFILE_SCHEMA_VERSION = 1

Confidence = Literal["high", "medium", "low"]
AuthorStatus = Literal["verified", "candidate", "uncertain", "not_found"]


@dataclass
class Keywords:
    core: list[str] = dataclasses.field(default_factory=list)
    broad: list[str] = dataclasses.field(default_factory=list)
    spelling_note: str = ""


@dataclass
class NegativeTerms:
    exclude: list[str] = dataclasses.field(
        default_factory=list
    )  # match without a core term cancels the channel
    penalty: list[str] = dataclasses.field(default_factory=list)


@dataclass
class CodeSet:
    """MSC codes or arXiv categories split by weight."""

    primary: list[str] = dataclasses.field(default_factory=list)
    secondary: list[str] = dataclasses.field(default_factory=list)


@dataclass
class Journal:
    name: str
    issn: list[str] = dataclasses.field(default_factory=list)
    openalex_source_id: str | None = None
    mode: Literal["full", "topic_filtered"] = "topic_filtered"
    yearly_volume: int | None = None
    verified: bool = False


@dataclass
class ProfileRule:
    """YAML-defined rule. ``when`` keys are interpreted by ``scoring.rules.loader``
    (L4):
    ``title_regex``, ``text_regex``, ``abstract_regex``, ``field_class``, ``not_channel_core``,
    ``not_author_status``, ``context_channel``, ``venue_legit``, ``all``, ``any``, ``not``."""

    id: str
    stage: RuleStage = RuleStage.DROP
    when: dict[str, Any] = dataclasses.field(default_factory=dict)
    reason: str | None = None


@dataclass
class Channel:
    """One topic channel. ``folder`` is the archive and notes
    sub-folder (single source)."""

    id: str = dataclasses.field(metadata={"pattern": r"^[A-Za-z][A-Za-z0-9_-]{0,15}$"})
    name: str
    folder: str = dataclasses.field(metadata={"pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$"})
    priority: int = dataclasses.field(default=1, metadata={"minimum": 1, "maximum": 3})
    keywords: Keywords = dataclasses.field(default_factory=Keywords)
    negative: NegativeTerms = dataclasses.field(default_factory=NegativeTerms)
    msc: CodeSet = dataclasses.field(default_factory=CodeSet)
    arxiv: CodeSet = dataclasses.field(default_factory=CodeSet)
    journals: list[Journal] = dataclasses.field(default_factory=list)
    noise_terms: list[str] = dataclasses.field(default_factory=list)
    rules: list[ProfileRule] = dataclasses.field(default_factory=list)
    citation_alone_passes: bool = False  # weekly: a seed citation alone passes


@dataclass
class AlwaysShowAuthor:
    """Always shown, +100 and mandatory pass; archived under ``persons/<folder>/``."""

    name: str
    orcid: str | None = None
    openalex_id: str | None = None
    aliases: list[str] = dataclasses.field(default_factory=list)
    folder: str | None = None  # null = derived from name by archive.naming


@dataclass
class FollowAuthor:
    name: str
    orcid: str | None = None
    openalex_id: str | None = None
    status: AuthorStatus = "candidate"
    channels: list[str] = dataclasses.field(default_factory=list)


@dataclass
class Authors:
    always_show: list[AlwaysShowAuthor] = dataclasses.field(default_factory=list)
    follow: list[FollowAuthor] = dataclasses.field(default_factory=list)


@dataclass
class Seed:
    title: str
    year: int | None = None
    first_author: str | None = None
    doi: str | None = None
    arxiv: str | None = None
    openalex_id: str | None = None
    channels: list[str] = dataclasses.field(default_factory=list)
    confidence: Confidence = "medium"
    source_files: list[str] = dataclasses.field(default_factory=list)


@dataclass
class Foundational:
    """Works cited by at least two seeds."""

    openalex_id: str
    title: str = ""
    cited_by_seeds: int = 2


@dataclass
class Profile:
    """Root of ``profile.yaml``."""

    schema_version: int = 1
    status: Literal["draft", "approved"] = "draft"
    approved_at: _dt.datetime | None = None
    language_hint: str = "en"
    field: str = ""
    channels: list[Channel] = dataclasses.field(default_factory=list)
    authors: Authors = dataclasses.field(default_factory=Authors)
    seeds: list[Seed] = dataclasses.field(default_factory=list)
    foundational: list[Foundational] = dataclasses.field(default_factory=list)
    excluded_topics: list[str] = dataclasses.field(default_factory=list)
    rules: list[ProfileRule] = dataclasses.field(default_factory=list)  # profile-wide rules
    open_problem_phrases: list[str] = dataclasses.field(default_factory=list)
    """Extra open-problem phrases (regexes, case-insensitive) of the user's field; ``process
    check`` adds them to the packaged ``open_problem_phrases.<lang>.json`` lists."""
    notes: str = ""

    def channel_by_id(self, channel_id: str) -> Channel | None:
        for ch in self.channels:
            if ch.id == channel_id:
                return ch
        return None

    @property
    def is_approved(self) -> bool:
        return self.status == "approved"


SCHEMA_REGISTRY["profile.schema.json"] = (
    Profile,
    "https://github.com/oselcukk/kallimachos/schemas/profile/1",
)
