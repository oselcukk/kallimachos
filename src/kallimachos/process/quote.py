"""Verbatim quote extraction (``process quote``).

NFC, ligature expansion, hyphenation join, control-character removal, whitespace collapse.
``process.check`` cleans pages with the same :func:`clean_text` before ``match_key``, so quote and
check can never disagree on normalisation.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from kallimachos.records.normalize import collapse_whitespace, nfc

DEFAULT_LENGTH = 300

LIGATURES = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "ﬅ": "ft",
    "ﬆ": "st",
    "Ĳ": "IJ",
    "ĳ": "ij",
}
_HYPHEN_BREAK = re.compile(r"(?<=\w)[-\u00ad\u2010]\s*\n\s*(?=\w)")
_SOFT_HYPHEN = "\u00ad"


@dataclass
class QuoteResult:
    text: str
    page: int
    start_offset: int
    end_offset: int
    exit_code: int  # 0 found, 1 not found, 2 usage


def expand_ligatures(text: str) -> str:
    return "".join(LIGATURES.get(ch, ch) for ch in text)


def strip_controls(text: str) -> str:
    """Drop C0/C1 control characters except the whitespace ones (collapsed later)."""
    return "".join(ch for ch in text if not (unicodedata.category(ch) == "Cc" and not ch.isspace()))


def clean_text(text: str) -> str:
    text = nfc(text)
    text = expand_ligatures(text)
    text = _HYPHEN_BREAK.sub("", text)
    text = text.replace(_SOFT_HYPHEN, "")
    text = strip_controls(text)
    return collapse_whitespace(text)


def phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Case-insensitive regex that matches ``phrase`` with any whitespace between its tokens."""
    tokens = [re.escape(tok) for tok in clean_text(phrase).split()]
    return re.compile(r"\s*".join(tokens) if tokens else r"(?!)", re.IGNORECASE)


def quote(
    pages: list[str], page: int, start: str, *, end: str | None = None, length: int | None = None
) -> QuoteResult:
    """Return the verbatim slice of the cleaned ``page`` that starts with ``start``.

    The slice ends with ``end`` (inclusive) when given, otherwise after ``length`` characters
    (default 300). Exit codes: 0 found, 1 not found, 2 usage (page out of range, empty start, both
    ``end`` and ``length``).
    """
    if end is not None and length is not None:
        return QuoteResult("", page, -1, -1, 2)
    if not start.strip() or (length is not None and length <= 0):
        return QuoteResult("", page, -1, -1, 2)
    if page < 1 or page > len(pages):
        return QuoteResult("", page, -1, -1, 2)
    text = clean_text(pages[page - 1])
    m = phrase_pattern(start).search(text)
    if m is None:
        return QuoteResult("", page, -1, -1, 1)
    begin = m.start()
    if end is not None:
        m_end = phrase_pattern(end).search(text, m.end())
        if m_end is None:
            return QuoteResult("", page, begin, -1, 1)
        stop = m_end.end()
    else:
        stop = min(len(text), begin + (length or DEFAULT_LENGTH))
    return QuoteResult(text[begin:stop].strip(), page, begin, stop, 0)
