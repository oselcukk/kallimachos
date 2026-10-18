"""``split-names``: fix creators with empty firstName and
a spaced lastName.

RIS import stores ``Given Surname`` as one ``lastName``; Better BibTeX keys then break. The
command selects such creators (at most four words, no digits or commas, not an institution) and
splits them; particles (``van``, ``de`` ...) stay with the surname.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

PARTICLES: tuple[str, ...] = (
    "van",
    "von",
    "de",
    "del",
    "della",
    "di",
    "da",
    "du",
    "le",
    "la",
    "der",
    "den",
    "ter",
    "ten",
    "al",
    "bin",
    "ibn",
    "dos",
    "das",
    "zu",
)
MAX_WORDS = 4
_INSTITUTION_RE = re.compile(
    r"\b(universit|institut|laborator|department|group|team|consortium|collaboration|society|"
    r"academy|college|school|centre|center|foundation|committee|association)",
    re.IGNORECASE,
)


def split_name(blob: str) -> tuple[str, str]:
    """Return (firstName, lastName); particles stay with the surname.

    ``"Surname, Given"`` is honoured as written. ``"Given Middle Surname"`` -> ``("Given Middle",
    "Surname")``; ``"Ludwig van Beethoven"`` -> ``("Ludwig", "van Beethoven")``. A single token is
    a surname only.
    """
    text = " ".join(blob.split())
    if "," in text:
        last, first = (part.strip() for part in text.split(",", 1))
        return first, last
    tokens = text.split(" ")
    if len(tokens) < 2:
        return "", text
    i = len(tokens) - 1
    while i > 1 and tokens[i - 1].lower().rstrip(".") in PARTICLES:
        i -= 1
    return " ".join(tokens[:i]), " ".join(tokens[i:])


def needs_split(creator: Mapping[str, Any]) -> bool:
    """A creator with an empty firstName and a multi-word lastName that looks like a person."""
    if creator.get("firstName", "") not in ("", None) or "name" in creator:
        return False
    last = str(creator.get("lastName", "")).strip()
    if " " not in last or "," in last or any(ch.isdigit() for ch in last):
        return False
    return len(last.split()) <= MAX_WORDS and not _INSTITUTION_RE.search(last)


def split_creators(data: dict[str, Any]) -> bool:
    """Split every creator of ``data`` that :func:`needs_split`; return whether anything changed."""
    changed = False
    creators = data.get("creators")
    if not isinstance(creators, list):
        return False
    for creator in creators:
        if isinstance(creator, dict) and needs_split(creator):
            first, last = split_name(str(creator["lastName"]))
            if first:
                creator["firstName"] = first
                creator["lastName"] = last
                changed = True
    return changed
