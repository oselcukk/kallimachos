"""UTF-16 message budget and the ``count`` command.

Telegram counts message length in UTF-16 code units (4096 hard limit). The message budget has a
target (3600) and a limit (3900) so that Hermes/Telegram headers still fit. When the rendered
message is over target the parts are shortened in this order: Why, then Summary, then near-miss
titles; items are never removed and no ellipsis is appended.

A message is described as a list of *parts* (``dict[str, str]``) so the shrinking logic does not
need to know the template language:

* ``{"kind": "text", "text": ...}``: fixed text, never shortened;
* ``{"kind": "item", "template": ..., "summary": ..., "why": ...}``: an item whose template
  contains :data:`SUMMARY_SLOT` and/or :data:`WHY_SLOT`;
* ``{"kind": "near", "template": ..., "title": ...}``: a near-miss entry whose template contains
  :data:`TITLE_SLOT`.

:func:`assemble` joins the parts without a separator (templates carry their own newlines).
"""

from __future__ import annotations

from dataclasses import dataclass

from kallimachos.interfaces import MessageBudget

TELEGRAM_HARD_LIMIT = 4096
SUMMARY_RANGE = (80, 220)
WHY_MAX_CHARS = 90

SUMMARY_SLOT = "\x00SUMMARY\x00"
WHY_SLOT = "\x00WHY\x00"
TITLE_SLOT = "\x00TITLE\x00"


@dataclass
class CountResult:
    chars: int
    utf16: int
    limit: int
    over: bool


def utf16_len(text: str) -> int:
    """Number of UTF-16 code units (what Telegram counts)."""
    return len(text.encode("utf-16-le")) // 2


def count(text: str, *, limit: int = TELEGRAM_HARD_LIMIT) -> CountResult:
    units = utf16_len(text)
    return CountResult(chars=len(text), utf16=units, limit=limit, over=units > limit)


def render_part(part: dict[str, str]) -> str:
    """Text of one part with its slots filled."""
    kind = part.get("kind", "text")
    if kind == "item":
        return (
            part["template"]
            .replace(SUMMARY_SLOT, part.get("summary", ""))
            .replace(WHY_SLOT, part.get("why", ""))
        )
    if kind == "near":
        return part["template"].replace(TITLE_SLOT, part.get("title", ""))
    return part.get("text", "")


def assemble(parts: list[dict[str, str]]) -> str:
    return "".join(render_part(p) for p in parts)


# (field, mode, value): mode "sentence" keeps the first sentence, "chars" caps the length.
_SHRINK_STEPS: tuple[tuple[str, str, int], ...] = (
    ("why", "sentence", 0),
    ("summary", "sentence", 0),
    ("title", "chars", 60),
    ("summary", "chars", 160),
    ("summary", "chars", 120),
    ("summary", "chars", SUMMARY_RANGE[0]),
    ("why", "chars", 60),
    ("title", "chars", 30),
    ("summary", "chars", 40),
    ("why", "chars", 30),
)

_FIELD_KIND = {"why": "item", "summary": "item", "title": "near"}


def fit(parts: list[dict[str, str]], budget: MessageBudget) -> list[dict[str, str]]:
    """Shrink Why -> Summary -> near misses until ``target_utf16`` is met.

    Returns new part dicts; the input is not modified. Items are never dropped. When even the last
    step does not reach the target, the shortest version is returned (callers report ``over``)."""
    from kallimachos.notify.format import first_sentence, shorten

    out = [dict(p) for p in parts]
    if utf16_len(assemble(out)) <= budget.target_utf16:
        return out
    for field_name, mode, value in _SHRINK_STEPS:
        for part in out:
            if part.get("kind") != _FIELD_KIND[field_name] or not part.get(field_name):
                continue
            text = part[field_name]
            part[field_name] = first_sentence(text) if mode == "sentence" else shorten(text, value)
        if utf16_len(assemble(out)) <= budget.target_utf16:
            break
    return out
