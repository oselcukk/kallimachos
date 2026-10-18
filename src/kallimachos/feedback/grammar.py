"""Reply grammar ``<n>[,<n>...] <tag> [: note]`` with en/tr synonyms.

* Items are message numbers: ``3`` (daily/weekly main list) or ``H2`` (weekly "from the people
  you follow" section). Several numbers are separated by commas, spaces or ``and``/``ve``.
* Tags are matched accent- and case-insensitively (Turkish synonyms with and without
  diacritics compare equal); both languages are always accepted, ``lang`` only orders the
  synonym tables. ``ask:<person>`` / ``sor:<person>`` names the person to ask. A leading ``#``
  is tolerated (``#good``).
* One message may hold several replies: one per line, or separated by ``;``.
* Daily and weekly items never mix inside one reply: ``3, H2 good`` is ambiguous and the reply
  is returned with ``ambiguous`` filled so the agent asks exactly one question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from kallimachos.interfaces import FeedbackTag
from kallimachos.records.normalize import match_key

SYNONYMS: dict[str, dict[FeedbackTag, tuple[str, ...]]] = {
    "en": {
        FeedbackTag.GOOD: ("good",),
        FeedbackTag.IRRELEVANT: ("irrelevant",),
        FeedbackTag.READ: ("read",),
        FeedbackTag.ASK: ("ask",),
        FeedbackTag.ARCHIVE: ("archive",),
    },
    "tr": {
        FeedbackTag.GOOD: ("iyi",),
        FeedbackTag.IRRELEVANT: ("alakas\u0131z", "ilgisiz"),
        FeedbackTag.READ: ("oku", "okuyaca\u011f\u0131m"),
        FeedbackTag.ASK: ("sor", "hocaya"),
        FeedbackTag.ARCHIVE: ("ar\u015fivle", "indir"),
    },
}

_ITEM_RE = re.compile(r"^(?:h)?[0-9]{1,3}$", re.I)
_CONNECTORS = frozenset({"and", "ve", "&", "+"})


@dataclass
class ParsedReply:
    items: list[str]  # "3", "H2"
    tag: FeedbackTag
    note: str | None = None
    ask_target: str | None = None  # ``ask:<person>``
    ambiguous: list[str] = field(default_factory=list)  # tokens that could not be resolved


def _tag_table(lang: str) -> dict[str, FeedbackTag]:
    """Normalised synonym -> tag; ``lang`` first so its words win on (unlikely) collisions."""
    order = [lang] + [k for k in SYNONYMS if k != lang]
    table: dict[str, FeedbackTag] = {}
    for code in order:
        for tag, words in SYNONYMS.get(code, {}).items():
            for word in words:
                table.setdefault(match_key(word), tag)
    return table


def _lookup_tag(token: str, table: dict[str, FeedbackTag]) -> tuple[FeedbackTag | None, str | None]:
    """(tag, ask target) for a token such as ``good``, ``#iyi`` or ``ask:someone``."""
    raw = token.strip().lstrip("#")
    word, sep, target = raw.partition(":")
    tag = table.get(match_key(word))
    if tag is None:
        return None, None
    if sep and target:
        return tag, (target.strip() if tag is FeedbackTag.ASK else None)
    return tag, None


def _normalise_item(token: str) -> str:
    token = token.strip().rstrip(".)")
    if token[:1].lower() == "h":
        return "H" + str(int(token[1:]))
    return str(int(token))


def _split_replies(text: str) -> list[str]:
    parts: list[str] = []
    for line in text.replace("\r", "\n").split("\n"):
        parts.extend(p for p in line.split(";") if p.strip())
    return parts


def _parse_one(chunk: str, table: dict[str, FeedbackTag]) -> ParsedReply | None:
    head, sep, note = chunk.partition(":")
    # ``ask:<person>`` also contains a colon; detect it before treating the rest as a note
    head_tokens = head.replace(",", " ").split()
    if head_tokens:
        tag, target = _lookup_tag(
            head_tokens[-1] + (":" + note.split()[0] if sep and note else ""), table
        )
        if tag is FeedbackTag.ASK and target:
            rest = note.split(maxsplit=1)
            trailing = rest[1] if len(rest) > 1 else ""
            reply = _build(head_tokens[:-1], tag, table)
            reply.ask_target = target
            reply.note = trailing.strip(" :\t") or None
            return reply
    tokens = head.replace(",", " ").split()
    if not tokens:
        return None
    tag_idx = next(
        (i for i, tok in enumerate(tokens) if _lookup_tag(tok, table)[0] is not None), None
    )
    if tag_idx is None:
        reply = ParsedReply(items=[], tag=FeedbackTag.GOOD, ambiguous=list(tokens))
        reply.note = note.strip() or None
        return reply
    tag, target = _lookup_tag(tokens[tag_idx], table)
    assert tag is not None
    item_tokens = tokens[:tag_idx] + tokens[tag_idx + 1 :]
    reply = _build(item_tokens, tag, table)
    reply.ask_target = target
    reply.note = note.strip() or None
    return reply


def _build(item_tokens: list[str], tag: FeedbackTag, table: dict[str, FeedbackTag]) -> ParsedReply:
    items: list[str] = []
    ambiguous: list[str] = []
    for tok in item_tokens:
        clean = tok.strip().rstrip(".)")
        if not clean or clean.lower() in _CONNECTORS:
            continue
        if _ITEM_RE.match(clean):
            item = _normalise_item(clean)
            if item not in items:
                items.append(item)
        elif _lookup_tag(clean, table)[0] is not None:
            ambiguous.append(clean)  # a second tag word in the same reply
        else:
            ambiguous.append(clean)
    weekly = [i for i in items if i.startswith("H")]
    if weekly and len(weekly) != len(items):
        ambiguous.extend(i for i in items if i not in ambiguous)  # daily and weekly mixed
    if not items:
        ambiguous.append("<no item number>")
    return ParsedReply(items=items, tag=tag, ambiguous=ambiguous)


def parse(text: str, *, lang: str = "en") -> list[ParsedReply]:
    """Parse one message into replies; both languages are always accepted."""
    table = _tag_table(lang)
    out: list[ParsedReply] = []
    for chunk in _split_replies(text):
        reply = _parse_one(chunk.strip(), table)
        if reply is not None:
            out.append(reply)
    return out


_MD_TICKED = re.compile(r"^\s*[-*]\s*\[[xX]\]\s*(.*)$")
_MD_TAG = re.compile(r"(?:#|->\s*)([^\s#:]+)(?::(\S+))?\s*$")


def markdown_tag(line: str) -> FeedbackTag | None:
    """Tag from a ticked checkbox line: ``#good`` or ``-> good`` at line end."""
    m = _MD_TICKED.match(line)
    if not m:
        return None
    body = m.group(1).rstrip()
    tag_match = _MD_TAG.search(body)
    if not tag_match:
        return None
    tag, _ = _lookup_tag(tag_match.group(1), _tag_table("en"))
    return tag
