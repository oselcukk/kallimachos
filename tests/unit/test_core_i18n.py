"""i18n.t and note_section_headings."""

from __future__ import annotations

import pytest

from kallimachos.i18n import NOTE_SECTION_KEYS, note_section_headings, t


def test_t_formats_placeholders() -> None:
    assert t("daily.header", "en", date="18 Oct 2026") == "Literature — 18 Oct 2026"
    assert t("failed", "tr", error="x").endswith("x")
    assert (
        t("queue.line", "en", title="T", url="U", kind="read", date="D") == "- [ ] T — U · read · D"
    )


def test_t_missing_placeholder_and_unknown_key() -> None:
    with pytest.raises(KeyError):
        t("daily.header", "en")
    with pytest.raises(KeyError):
        t("no.such.key")


def test_t_unknown_language_falls_back_to_english() -> None:
    assert t("partial", "xx") == t("partial", "en")


def test_note_section_headings_tr_in_order() -> None:
    headings = note_section_headings("tr")
    assert len(headings) == 11
    assert headings[0] == t("note.section.citation", "tr")
    assert headings[-1] == t("note.section.not_found", "tr")
    assert headings == [t(k, "tr") for k in NOTE_SECTION_KEYS]


def test_note_section_headings_en() -> None:
    assert note_section_headings() == [
        "Citation",
        "Summary",
        "Structure",
        "Definitions",
        "Main results",
        "Method",
        "Open problems and questions",
        "Relation to prior work",
        "Caveats",
        "Source file",
        "Not found",
    ]
