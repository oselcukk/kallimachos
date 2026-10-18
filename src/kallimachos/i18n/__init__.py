"""Message and heading catalogs.

``en.json`` and ``tr.json`` have identical key sets and identical placeholder sets per key
(tested). Unknown languages fall back to English.
"""

from __future__ import annotations

import json
from functools import cache
from importlib import resources

SUPPORTED: tuple[str, ...] = ("en", "tr")
DEFAULT = "en"


@cache
def load_catalog(lang: str) -> dict[str, str]:
    """Load ``<lang>.json`` from the package; unknown language -> English."""
    code = lang if lang in SUPPORTED else DEFAULT
    text = resources.files(__package__).joinpath(f"{code}.json").read_text(encoding="utf-8")
    data = json.loads(text)
    assert isinstance(data, dict)
    return {str(k): str(v) for k, v in data.items()}


NOTE_SECTION_KEYS: tuple[str, ...] = (
    "note.section.citation",
    "note.section.summary",
    "note.section.structure",
    "note.section.definitions",
    "note.section.main_results",
    "note.section.method",
    "note.section.open_problems",
    "note.section.prior_work",
    "note.section.caveats",
    "note.section.source_file",
    "note.section.not_found",
)
"""The eleven numbered note sections in order. ``note.section.relation`` ("Relation
to my work") is optional and unnumbered, so it is not part of this list."""


def t(key: str, lang: str = DEFAULT, **values: object) -> str:
    """Translate ``key`` and format placeholders. Missing
    placeholders raise ``KeyError``.

    An unknown key raises ``KeyError`` as well; a key missing from a non-English catalog falls
    back to English (the catalogs are kept equal by tests, this is only a safety net).
    """
    catalog = load_catalog(lang)
    template = catalog.get(key)
    if template is None:
        template = load_catalog(DEFAULT).get(key)
    if template is None:
        raise KeyError(f"unknown i18n key {key!r}")
    return template.format_map(values)


def note_section_headings(lang: str = DEFAULT) -> list[str]:
    """Ordered note section headings (``note.section.*`` keys)."""
    return [t(key, lang) for key in NOTE_SECTION_KEYS]
