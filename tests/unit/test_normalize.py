"""records.normalize: NFC storage standard and match_key."""

from __future__ import annotations

import unicodedata

import pytest

from kallimachos.records.normalize import collapse_whitespace, match_key, nfc, strip_accents


def test_nfc_composes() -> None:
    decomposed = "e\u0301"  # e + combining acute
    assert nfc(decomposed) == "\u00e9"
    assert unicodedata.is_normalized("NFC", nfc("Gau\u00df \u0130stanbul"))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Riemann Zeta-Function", "riemann zeta function"),
        ("  Dirichlet   L-functions ", "dirichlet l functions"),
        ("\u0130stanbul \u0131\u015f\u0131k \u00e7\u011f", "istanbul isik cg"),
        ("Erd\u0151s\u2013R\u00e9nyi", "erdos renyi"),
        ("\ufb01nite \ufb02ow", "finite flow"),  # ligatures decompose under NFKD
        ("Gau\u00df", "gau\u00df"),  # sharp s has no decomposition and is kept
        ("x\u00b2 + y_1 = z'", "x2 y 1 z"),
        ("", ""),
    ],
)
def test_match_key(raw: str, expected: str) -> None:
    assert match_key(raw) == expected


def test_match_key_is_idempotent_and_nfc_invariant() -> None:
    text = "Gro\u00dfe Primzahll\u00fccken \u2014 \u00dcberblick"
    key = match_key(text)
    assert match_key(key) == key
    assert match_key(unicodedata.normalize("NFD", text)) == key
    assert match_key(unicodedata.normalize("NFKC", text)) == key


def test_strip_accents_handles_dotted_and_dotless_i() -> None:
    assert strip_accents("\u0130\u0131") == "Ii"


def test_collapse_whitespace() -> None:
    assert collapse_whitespace("  a \n\t b  ") == "a b"
