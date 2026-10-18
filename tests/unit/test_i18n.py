"""i18n catalogs: identical keys and placeholders in en/tr."""

from __future__ import annotations

import re

from kallimachos.i18n import SUPPORTED, load_catalog

PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def test_key_sets_equal() -> None:
    catalogs = {lang: load_catalog(lang) for lang in SUPPORTED}
    keys = {lang: set(c) for lang, c in catalogs.items()}
    assert keys["en"] == keys["tr"], keys["en"] ^ keys["tr"]


def test_placeholders_equal_per_key() -> None:
    en, tr = load_catalog("en"), load_catalog("tr")
    for key in en:
        assert set(PLACEHOLDER.findall(en[key])) == set(PLACEHOLDER.findall(tr[key])), key


def test_unknown_language_falls_back_to_english() -> None:
    assert load_catalog("xx") == load_catalog("en")


def test_appendix_d_keys_present() -> None:
    required = {
        "daily.header",
        "weekly.header",
        "item",
        "item.archived",
        "item.no_summary",
        "near_miss",
        "reply_hint",
        "empty.daily",
        "empty.weekly",
        "failed",
        "partial",
        "source_note",
        "persons.header",
        "feedback.ack",
        "feedback.extra.read",
        "feedback.extra.archive",
        "archive.summary",
    }
    assert required <= set(load_catalog("en"))
