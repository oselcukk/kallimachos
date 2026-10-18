"""Text normalisation, the single place for it.

* Storage standard is NFC: every writer passes strings through :func:`nfc` (``state.store`` enforces
it).
* Matching/search uses :func:`match_key`: NFKD -> strip combining marks -> dotless i to i ->
  lower-case -> every non-alphanumeric run becomes one space -> trimmed.

``process.check`` compares quotes with :func:`match_key` too, so quote and check can never
disagree on normalisation.
"""

from __future__ import annotations

import re
import unicodedata

_NON_ALNUM = re.compile(r"[\W_]+")  # Unicode-aware: letters without a decomposition (ß, ø) survive
_WS = re.compile(r"\s+")


def nfc(text: str) -> str:
    """Canonical composition (storage standard)."""
    return unicodedata.normalize("NFC", text)


def strip_accents(text: str) -> str:
    """NFKD decomposition with combining marks removed. ``ı`` (dotless i) has no decomposition,
    so it is mapped explicitly; ``İ`` decomposes to ``I`` + combining dot above, which is dropped.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return without_marks.replace("ı", "i")


def match_key(text: str) -> str:
    """Matching key: accent-free, lower-case, alphanumeric tokens separated by single spaces."""
    lowered = strip_accents(text).lower()
    return _WS.sub(" ", _NON_ALNUM.sub(" ", lowered)).strip()


def collapse_whitespace(text: str) -> str:
    """Single spaces, no leading/trailing whitespace. Does not touch case or accents."""
    return _WS.sub(" ", text).strip()
