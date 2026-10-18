"""Deterministic fallback provider.

Selection = first ``main_items`` of the ranking + next ``near_miss`` (certain-pass candidates always
in ``main``; weekly: followed-author items fixed on top, no near misses); summary = first sentences
of the source abstract (``notify.format.shorten``); why = the strongest one or two ``score_reasons``
(scoring orders them strongest first) with numbers removed.
"""

from __future__ import annotations

import re
from typing import Any

from kallimachos.config.schema import RankingConfig
from kallimachos.interfaces import (
    Candidate,
    CheckResult,
    RunKind,
    RunResult,
    Selection,
    UsageError,
)
from kallimachos.notify.budget import WHY_MAX_CHARS

_NUMBER_TOKEN = re.compile(
    r"(?:^|(?<=\s))[(\[]?[+\-=\u00d7x*]?\d+(?:[.,]\d+)?%?[)\]]?(?=\s|$|[,;:])", re.IGNORECASE
)
_SCORE_WORDS = re.compile(r"\b(?:score|points?|pts)\b", re.IGNORECASE)


class NoneProvider:
    name = "none"

    def complete(self, prompt: str, *, timeout_s: int, json_schema: dict[str, Any] | None) -> str:
        """Never called for selection; raises ``UsageError`` to make misuse visible."""
        raise UsageError("model.provider 'none' makes no model call (deterministic selection)")

    def test(self) -> CheckResult:
        return CheckResult(ok=True, message="deterministic provider; no model call")


def _pick(ordered: list[Candidate], fixed: list[Candidate], n: int) -> list[Candidate]:
    chosen = {c.id for c in fixed}
    for cand in ordered:
        if len(chosen) >= n:
            break
        chosen.add(cand.id)
    return [c for c in ordered if c.id in chosen]


def deterministic_selection(run: RunResult, cfg: RankingConfig) -> Selection:
    from kallimachos.model.select import build_selection

    ordered = sorted(run.candidates, key=lambda c: c.no)
    if run.kind is RunKind.WEEKLY:
        persons = [c for c in ordered if c.always_show_author]
        rest = [c for c in ordered if not c.always_show_author]
        main = _pick(rest, [c for c in rest if c.certain_pass], cfg.main_items)
        return build_selection(run, [c.id for c in persons + main], [], by="rule")
    main = _pick(ordered, [c for c in ordered if c.certain_pass], cfg.main_items)
    main_ids = {c.id for c in main}
    near = [c for c in ordered if c.id not in main_ids][: cfg.near_miss]
    return build_selection(run, [c.id for c in main], [c.id for c in near], by="rule")


def deterministic_summary(cand: Candidate, *, max_chars: int, lang: str) -> str:
    """First (at most two) sentences of the abstract, cut within ``max_chars``; empty when the
    source has no abstract (the renderer then prints the "no summary" line)."""
    from kallimachos.notify.format import clean_text, first_sentence, shorten

    text = clean_text(cand.abstract or "")
    if not text:
        return ""
    first = first_sentence(text)
    rest = text[len(first) :].strip()
    two = f"{first} {first_sentence(rest)}" if rest else first
    return shorten(two, max_chars)


def strip_numbers(text: str) -> str:
    """Remove stand-alone numeric tokens (scores, weights, counts) and score words."""
    text = _NUMBER_TOKEN.sub("", text)
    text = _SCORE_WORDS.sub("", text)
    text = re.sub(r"\(\s*\)|\[\s*\]", "", text)
    return re.sub(r"\s+", " ", text).strip(" ,;:")


def deterministic_why(cand: Candidate, *, lang: str) -> str:
    """Template sentence from the strongest one or two signals; never a number or score."""
    from kallimachos.notify.format import shorten

    parts = [r for r in (strip_numbers(x) for x in cand.score_reasons) if r][:2]
    if not parts and cand.channel_names:
        parts.append(", ".join(cand.channel_names[:2]))
    if cand.near_note is not None:
        parts.append(cand.near_note.note_ref or cand.near_note.seed)
    if cand.note:
        parts.append(cand.note)
    why = "; ".join(p for p in parts if p)
    if why:
        why = why[0].upper() + why[1:]
    return shorten(why, WHY_MAX_CHARS)
