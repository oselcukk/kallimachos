"""Card verification against the text (``process check``).

Quotes: ``match_key`` + ``rapidfuzz.partial_ratio`` >= threshold (declared page, then all pages
-> ``page_fixed``, then neighbouring page pairs). Scope regexes, open-problem phrases, reference
count estimate, schema. ``importance``: ``quoted_from_other AND type == definition -> supporting``.
Result: > 5 % quotes not found, heavy omissions or schema errors -> ``check_failed``.

The function never writes; ``cli_process.cmd_check`` records the state unless ``--pre``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import jsonschema
from rapidfuzz import fuzz

from kallimachos.config.schema import ProcessConfig
from kallimachos.data import load_json
from kallimachos.interfaces import Card, ItemType, dump
from kallimachos.process.card import CARD_SCHEMA, OPEN_TYPES, item_rule_errors
from kallimachos.process.quote import clean_text
from kallimachos.records.normalize import match_key, nfc

NOT_FOUND_FAIL_RATIO = 0.05
REFERENCE_COUNT_TOLERANCE = 0.20

SCOPE_WORDS: tuple[str, ...] = (
    "Definition",
    "Theorem",
    "Proposition",
    "Lemma",
    "Corollary",
    "Conjecture",
    "Problem",
    "Question",
    "Hypothesis",
)
HEAVY_WORDS: frozenset[str] = frozenset({"conjecture", "problem", "question", "hypothesis"})
_WORD_TO_TYPE: dict[str, ItemType] = {
    "definition": ItemType.DEFINITION,
    "theorem": ItemType.THEOREM,
    "proposition": ItemType.PROPOSITION,
    "lemma": ItemType.LEMMA,
    "corollary": ItemType.COROLLARY,
    "conjecture": ItemType.CONJECTURE,
    "problem": ItemType.OPEN_PROBLEM,
    "question": ItemType.QUESTION,
    "hypothesis": ItemType.CONJECTURE,
}
_WORDS = "|".join(SCOPE_WORDS)
_NUMBER = r"[0-9]+(?:[.\-][0-9]+)*[a-z]?|[A-Z](?:\.[0-9]+)+"
HEADING_RE = re.compile(
    rf"^[ \t]*(?:\*\*|__)?(?P<word>{_WORDS})[ \t]+(?P<number>{_NUMBER})[ \t]*"
    rf"(?:[.:(\[\u2014\u2013]|\*\*|__|$)",
    re.MULTILINE,
)
"""A heading counts only at line start, with its whole number and a separator after it."""
_SPLIT_NUMBER_RE = re.compile(rf"\b((?:{_WORDS})[ \t]+[0-9]+)\.[ \t]*\n[ \t]*([0-9]+)\b")
_SENTENCE_END = (".", "?", "!", ":", ";")
REFERENCES_RE = re.compile(
    r"^[ \t]*(?:[0-9]+[.)]?[ \t]+)?(References|Bibliography|Literature(?: cited)?|"
    r"Literatur\w*|Works cited)[ \t]*\.?[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)
_REF_BRACKET = re.compile(r"^[ \t]*\[[0-9]+\]")
_REF_NUMBERED = re.compile(r"^[ \t]*[0-9]+\.[ \t]+\S")
_REF_YEAR = re.compile(r"^[ \t]*[A-ZÀ-ɏ].*\b(?:1[89][0-9]{2}|20[0-9]{2})\b")

COVERING_TYPES: frozenset[ItemType] = OPEN_TYPES | {ItemType.CLAIMED_SOLUTION}
"""Items that cover an open-problem phrase on their page."""


@dataclass
class QuoteCheck:
    item_index: int
    found: bool
    page: int
    page_fixed: int | None = None
    score: float = 0.0


@dataclass
class ScopeHeading:
    word: str  # lower-case scope word
    number: str
    page: int  # 1-based text page

    @property
    def label(self) -> str:
        return f"{self.word.capitalize()} {self.number}"


@dataclass
class CheckReport:
    eid: str
    quotes: list[QuoteCheck] = field(default_factory=list)
    scope_warnings: list[str] = field(default_factory=list)
    missing_items: list[str] = field(default_factory=list)
    reference_count_estimate: int | None = None
    schema_errors: list[str] = field(default_factory=list)
    result: Literal["check_passed", "check_failed"] = "check_failed"
    heavy_missing: list[str] = field(default_factory=list)
    not_found_ratio: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- quotes ----------------------------------------------------------------------------------


def page_keys(pages: Sequence[str]) -> list[str]:
    """``match_key(clean_text(page))`` for every page (the normalisation ``quote`` used)."""
    return [match_key(clean_text(p)) for p in pages]


def _score(needle: str, hay: str) -> float:
    if not needle or not hay:
        return 0.0
    return float(fuzz.partial_ratio(needle, hay))


def find_quote(
    pages: list[str], quote: str, declared_page: int, *, threshold: int, keys: Sequence[str] = ()
) -> QuoteCheck:
    """Locate ``quote``: declared page, then every page (``page_fixed``), then page pairs."""
    keys = list(keys) or page_keys(pages)
    needle = match_key(clean_text(quote))
    n = len(keys)
    best_page = declared_page
    best = 0.0
    if 1 <= declared_page <= n:
        best = _score(needle, keys[declared_page - 1])
        if best >= threshold:
            return QuoteCheck(-1, True, declared_page, None, best)
    for i, key in enumerate(keys, start=1):
        if i == declared_page:
            continue
        s = _score(needle, key)
        if s > best:
            best, best_page = s, i
    if best >= threshold:
        return QuoteCheck(-1, True, declared_page, best_page, best)
    for i in range(1, n):
        s = _score(needle, keys[i - 1] + " " + keys[i])
        if s >= threshold:
            fixed = None if declared_page in (i, i + 1) else i
            return QuoteCheck(-1, True, declared_page, fixed, s)
        if s > best:
            best, best_page = s, i
    return QuoteCheck(-1, False, declared_page, None, best)


# --- scope -----------------------------------------------------------------------------------


def join_split_numbers(text: str) -> str:
    """``Problem 5.\\n4`` -> ``Problem 5.4`` (a number broken across a line end)."""
    return _SPLIT_NUMBER_RE.sub(r"\1.\2", text)


def references_start(pages: Sequence[str]) -> tuple[int, int] | None:
    """``(page_index, char_offset)`` of the last References/Bibliography heading, or None."""
    found: tuple[int, int] | None = None
    for i, page in enumerate(pages):
        for m in REFERENCES_RE.finditer(page):
            found = (i, m.start())
    return found


def _previous_line_mid_sentence(text: str, offset: int) -> bool:
    before = text[:offset].rstrip("\n")
    lines = [ln for ln in before.split("\n") if ln.strip()]
    if not lines:
        return False
    prev = lines[-1].rstrip()
    if HEADING_RE.match(prev) or prev.endswith(_SENTENCE_END) or prev.endswith((")", "]")):
        return False
    gap = text[len(before) : offset].count("\n")
    # a blank line separates paragraphs: the heading then starts a new paragraph
    return gap < 2


def scope_headings(pages: Sequence[str]) -> list[ScopeHeading]:
    """Numbered statement headings at line/paragraph start, excluding the reference list."""
    ref_start = references_start(pages)
    out: list[ScopeHeading] = []
    for i, raw in enumerate(pages):
        text = join_split_numbers(nfc(raw))
        for m in HEADING_RE.finditer(text):
            if ref_start is not None and (i, m.start()) >= ref_start:
                continue
            if _previous_line_mid_sentence(text, m.start()):
                continue
            out.append(ScopeHeading(m.group("word").lower(), m.group("number").rstrip("."), i + 1))
    return out


def _norm_number(number: str | None) -> str:
    return (number or "").strip().rstrip(".").lower()


def heading_covered(card: Card, heading: ScopeHeading) -> bool:
    expected = _WORD_TO_TYPE[heading.word]
    for item in card.items:
        if _norm_number(item.number) == heading.number.lower() and (
            item.type is expected or heading.word in item.label.lower()
        ):
            return True
    for fa in card.false_alarms:
        if fa.page == heading.page and heading.label.lower() in fa.phrase.lower():
            return True
    return False


# --- open-problem phrases ----------------------------------------------------------------------


def load_open_problem_patterns(
    langs: Iterable[str] = ("en",), extra: Iterable[str] = ()
) -> list[re.Pattern[str]]:
    """Packaged phrase lists for ``langs`` plus the profile's ``open_problem_phrases`` (``extra``;
    an invalid regex is matched literally)."""
    patterns: list[re.Pattern[str]] = []
    for lang in langs:
        try:
            data = load_json(f"open_problem_phrases.{lang}.json")
        except FileNotFoundError:
            continue  # no phrase list for this language yet; the English one always applies
        for pat in data.get("patterns", []):
            patterns.append(re.compile(pat, re.IGNORECASE))
    for phrase in extra:
        if not phrase.strip():
            continue
        try:
            patterns.append(re.compile(phrase, re.IGNORECASE))
        except re.error:
            patterns.append(re.compile(re.escape(phrase.strip()), re.IGNORECASE))
    return patterns


def phrase_covered(card: Card, page: int) -> bool:
    return any(item.page == page and item.type in COVERING_TYPES for item in card.items) or any(
        fa.page == page for fa in card.false_alarms
    )


def open_problem_hits(
    pages: Sequence[str], patterns: Sequence[re.Pattern[str]]
) -> list[tuple[int, str]]:
    """``(page, matched phrase)`` for every open-problem phrase outside the reference list."""
    ref_start = references_start(pages)
    hits: list[tuple[int, str]] = []
    for i, raw in enumerate(pages):
        text = clean_text(raw)
        if ref_start is not None and i > ref_start[0]:
            continue
        for pat in patterns:
            for m in pat.finditer(text):
                hits.append((i + 1, m.group(0)))
    return hits


# --- references ------------------------------------------------------------------------------


def estimate_reference_count(pages: Sequence[str]) -> int | None:
    start = references_start(pages)
    if start is None:
        return None
    page_i, offset = start
    tail = pages[page_i][offset:].split("\n", 1)[1] if "\n" in pages[page_i][offset:] else ""
    lines = tail.split("\n")
    for page in pages[page_i + 1 :]:
        lines.extend(page.split("\n"))
    for pattern in (_REF_BRACKET, _REF_NUMBERED, _REF_YEAR):
        count = sum(1 for ln in lines if pattern.match(ln))
        if count:
            return count
    return 0


# --- the check -------------------------------------------------------------------------------


def schema_errors(card: Card, pages: Sequence[str]) -> list[str]:
    errors: list[str] = []
    validator = jsonschema.Draft202012Validator(CARD_SCHEMA)
    for err in validator.iter_errors(dump(card)):
        path = ".".join(str(p) for p in err.absolute_path)
        errors.append(f"{path}: {err.message}")
    errors.extend(item_rule_errors(card))
    for i, item in enumerate(card.items):
        if item.page < 1 or item.page > len(pages):
            errors.append(f"items[{i}]: page {item.page} outside 1-{len(pages)}")
    return errors


def check_card(
    card: Card,
    pages: list[str],
    cfg: ProcessConfig,
    *,
    pre: bool = False,
    langs: Sequence[str] = ("en",),
    extra_phrases: Sequence[str] = (),
) -> CheckReport:
    """Verify ``card`` against ``pages``. ``pre`` is informational: this function never writes.
    ``extra_phrases`` are the profile's open-problem phrases (``Profile.open_problem_phrases``)."""
    del pre
    report = CheckReport(eid=card.eid)
    keys = page_keys(pages)
    threshold = cfg.quote_threshold
    for i, item in enumerate(card.items):
        qc = find_quote(pages, item.quote, item.page, threshold=threshold, keys=keys)
        qc.item_index = i
        report.quotes.append(qc)
    not_found = sum(1 for q in report.quotes if not q.found)
    report.not_found_ratio = not_found / len(report.quotes) if report.quotes else 0.0

    for heading in scope_headings(pages):
        if heading_covered(card, heading):
            continue
        text = f"{heading.label} (p. {heading.page})"
        report.missing_items.append(text)
        if heading.word in HEAVY_WORDS:
            report.heavy_missing.append(text)

    patterns = load_open_problem_patterns(langs, extra_phrases)
    for page, phrase in open_problem_hits(pages, patterns):
        if phrase_covered(card, page):
            continue
        text = f"open-problem phrase {phrase!r} on p. {page} not covered by an item or false_alarm"
        report.scope_warnings.append(text)
        report.heavy_missing.append(text)

    estimate = estimate_reference_count(pages)
    report.reference_count_estimate = estimate
    if estimate:
        diff = abs(estimate - len(card.references))
        if diff > max(2, int(REFERENCE_COUNT_TOLERANCE * estimate)):
            report.scope_warnings.append(
                f"reference count: card has {len(card.references)}, text suggests about {estimate}"
            )

    report.schema_errors = schema_errors(card, pages)
    failed = (
        report.not_found_ratio > NOT_FOUND_FAIL_RATIO
        or bool(report.heavy_missing)
        or bool(report.schema_errors)
    )
    report.result = "check_failed" if failed else "check_passed"
    return report
