"""File naming.

``{authors}_{year}_{short_title}[_arXiv-{arxiv}{version}].pdf``; surnames joined by ``-``,
more than ``max_authors_in_name`` -> ``First-etal`` (``vd`` only for Turkish users who ask);
surname normalisation (Jr/Sr dropped, particles lower-cased and joined, ALL CAPS -> Title Case,
ASCII transliteration + NFKD, compound surnames from ``data/compound_surnames.json``); short title
keeps ``$...$``, drops LaTeX commands and leading articles, <= 7 words / <= 60 chars.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from kallimachos.archive.identity import journal_doi, split_arxiv_version
from kallimachos.config.schema import ArchiveConfig, Config
from kallimachos.data import load_json
from kallimachos.interfaces import Record
from kallimachos.records.normalize import collapse_whitespace, match_key, nfc, strip_accents

SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,220}\.pdf$")
UNDATED = "undated"
ANONYMOUS = "Anonymous"
UNTITLED = "Untitled"
DEFAULT_ETAL = "etal"
TURKISH_ETAL = "vd"

# Letters that NFKD does not decompose to ASCII (escaped: the repository keeps non-English
# characters out of code, see tests/test_repo_hygiene.py).
TRANSLIT: dict[str, str] = {
    "\u00df": "ss",  # sharp s
    "\u00e6": "ae",
    "\u00c6": "Ae",
    "\u0153": "oe",
    "\u0152": "Oe",
    "\u00f8": "o",
    "\u00d8": "O",
    "\u0111": "d",
    "\u0110": "D",
    "\u00f0": "d",
    "\u00d0": "D",
    "\u0142": "l",
    "\u0141": "L",
    "\u00fe": "th",
    "\u00de": "Th",
    "\u0131": "i",  # dotless i
    "\u0130": "I",  # dotted capital I
}

SMALL_WORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "and",
        "or",
        "on",
        "in",
        "at",
        "to",
        "for",
        "by",
        "from",
        "with",
        "via",
        "over",
        "into",
        "nor",
        "as",
        "vs",
    ]
)
ARTICLES = frozenset({"a", "an", "the"})
_PART_SPLIT = re.compile("[-'\u2019\u2010\u2011]")


@dataclass(frozen=True)
class SurnameData:
    particles: frozenset[str]
    prefixes: frozenset[str]
    connectors: frozenset[str]
    suffixes: frozenset[str]
    compound: frozenset[str]


@lru_cache(maxsize=1)
def surname_data() -> SurnameData:
    raw = load_json("compound_surnames.json")
    return SurnameData(
        particles=frozenset(raw["particles"]),
        prefixes=frozenset(raw["compound_prefixes"]),
        connectors=frozenset(raw["double_surname_connectors"]),
        suffixes=frozenset(raw["suffixes"]),
        compound=frozenset(match_key(c) for c in raw["compound_surnames"]),
    )


def to_ascii(text: str) -> str:
    """Transliteration table first, then NFKD with combining marks removed."""
    return strip_accents("".join(TRANSLIT.get(ch, ch) for ch in nfc(text)))


def alnum(text: str) -> str:
    """ASCII letters and digits only."""
    return re.sub(r"[^A-Za-z0-9]", "", to_ascii(text))


def _bare(token: str) -> str:
    return token.strip(".,;").lower()


def _case_token(token: str) -> str:
    """ALL CAPS -> Title Case, all lower -> capitalised, mixed case kept; parts split on
    hyphen/apostrophe are cased separately and joined without separator."""
    out: list[str] = []
    for part in (p for p in _PART_SPLIT.split(token) if p):
        letters = [c for c in part if c.isalpha()]
        if len(letters) > 1 and all(c.isupper() for c in letters):
            part = part[0] + part[1:].lower()
        elif letters and all(c.islower() for c in letters):
            part = part[0].upper() + part[1:]
        out.append(part)
    return "".join(out)


def surname(name: str, *, compound: frozenset[str] = frozenset()) -> str:
    """ASCII surname for a file name.

    ``Family, Given`` keeps the whole family part. Otherwise the last token, extended backwards
    over particles (``van der``), compound prefixes (``Saint``) and a double-surname connector
    (``A y B``); a known compound surname (packaged list or ``compound``) wins. The first token
    is always treated as the given name.
    """
    data = surname_data()
    compound_keys = data.compound | frozenset(match_key(c) for c in compound)
    text = collapse_whitespace(nfc(name))
    if "," in text:
        family = text.partition(",")[0]
        tokens = [t for t in family.split() if _bare(t) not in data.suffixes]
        return _join_surname(tokens, data)
    tokens = [t for t in text.split() if t.strip(".,")]
    while len(tokens) > 1 and _bare(tokens[-1]) in data.suffixes:
        tokens.pop()
    if not tokens:
        return ""
    for k in range(len(tokens) - 1, 1, -1):
        if match_key(" ".join(tokens[-k:])) in compound_keys:
            return _join_surname(tokens[-k:], data)
    start = len(tokens) - 1
    if start >= 3 and _bare(tokens[start - 1]) in data.connectors:
        start -= 2
    while start >= 2 and (
        _bare(tokens[start - 1]) in data.particles or _bare(tokens[start - 1]) in data.prefixes
    ):
        start -= 1
    return _join_surname(tokens[start:], data)


def _join_surname(tokens: list[str], data: SurnameData) -> str:
    out: list[str] = []
    for i, token in enumerate(tokens):
        bare = _bare(token)
        if i < len(tokens) - 1 and bare in data.particles:
            out.append(alnum(bare).lower())
        else:
            out.append(alnum(_case_token(token.strip(".,"))))
    return "".join(out)


def etal_suffix_for(config: Config) -> str:
    """``notes.etal_suffix`` (default ``etal``); ``vd`` is honoured only when the language is
    Turkish, otherwise ``etal``. Unsafe characters are removed."""
    lang = (config.notes.language or config.language or "en").lower()
    suffix = alnum(config.notes.etal_suffix) or DEFAULT_ETAL
    if suffix.lower() == TURKISH_ETAL and not lang.startswith("tr"):
        return DEFAULT_ETAL
    return suffix


def authors_part(authors: list[str], cfg: ArchiveConfig, *, etal_suffix: str = "etal") -> str:
    names = [s for s in (surname(a) for a in authors) if s]
    if not names:
        return ANONYMOUS
    if len(names) > cfg.max_authors_in_name:
        return f"{names[0]}-{etal_suffix}"
    return "-".join(names)


_MATH_RE = re.compile(r"\$\$?(.+?)\$\$?")
_CMD_RE = re.compile(r"\\[A-Za-z]+\*?|\\.")
_WORD_SPLIT = re.compile('[\\s\\-/\u2010\u2011\u2013\u2014:;,.!?()\\[\\]{}"]+')


_MATH_FORMAT_CMDS = frozenset(
    [
        "mathbb",
        "mathcal",
        "mathfrak",
        "mathrm",
        "mathbf",
        "mathit",
        "mathsf",
        "mathscr",
        "operatorname",
        "text",
        "textrm",
        "textit",
        "textbf",
        "left",
        "right",
        "big",
        "Big",
        "bigl",
        "bigr",
        "Bigl",
        "Bigr",
        "frac",
        "dfrac",
        "tfrac",
        "sqrt",
        "hat",
        "bar",
        "tilde",
        "overline",
        "underline",
        "widehat",
        "widetilde",
        "cdot",
        "cdots",
        "ldots",
        "dots",
        "quad",
        "qquad",
        "displaystyle",
    ]
)


def _math_text(math: str) -> str:
    """Keep inline math as text: formatting commands go, symbol names stay
    (``\\mathbb{Z}_p`` -> ``Zp``, ``\\alpha`` -> ``alpha``)."""

    def _cmd(m: re.Match[str]) -> str:
        name = m.group(0)[1:].rstrip("*")
        return "" if name in _MATH_FORMAT_CMDS or not name.isalpha() else name

    return alnum(_CMD_RE.sub(_cmd, math))


def short_title(title: str, *, max_words: int, max_chars: int) -> str:
    text = collapse_whitespace(nfc(title))
    maths: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        maths.append(_math_text(m.group(1)))
        return f" \x00{len(maths) - 1}\x00 "

    text = _MATH_RE.sub(_stash, text)  # inline math survives as one word
    text = _CMD_RE.sub(" ", text)  # commands outside math are dropped, their arguments kept
    words: list[tuple[str, bool]] = []  # (word, is_math)
    for raw in _WORD_SPLIT.split(text):
        m = re.fullmatch(r"\x00(\d+)\x00", raw)
        if m:
            if maths[int(m.group(1))]:
                words.append((maths[int(m.group(1))], True))
            continue
        w = alnum(raw)
        if w:
            words.append((w, False))
    while words and not words[0][1] and words[0][0].lower() in ARTICLES:
        words.pop(0)
    out: list[str] = []
    for i, (word, is_math) in enumerate(words):
        if is_math:
            out.append(word)
        elif i > 0 and word.lower() in SMALL_WORDS:
            out.append(word.lower())
        elif word.isupper() and len(word) > 4 and not any(c.isdigit() for c in word):
            out.append(word[0] + word[1:].lower())
        else:
            out.append(word[0].upper() + word[1:])
    out = out[:max_words]
    while out and len("-".join(out)) > max_chars:
        out.pop()
    while len(out) > 1 and out[-1].lower() in SMALL_WORDS:
        out.pop()
    if not out:
        return words[0][0][:max_chars] if words else UNTITLED
    return "-".join(out)


def year_part(record: Record) -> str:
    """Journal year if present, else first-version year, else ``undated``."""
    if journal_doi(record) and record.published:
        return str(record.published.year)
    if record.created:
        return str(record.created.year)
    if record.published:
        return str(record.published.year)
    return UNDATED


def arxiv_name_part(arxiv_id: str) -> str:
    """Old-style ids contain ``/``; it becomes ``-`` in file names."""
    return re.sub(r"[^A-Za-z0-9.-]", "-", arxiv_id)


def build_filename(
    record: Record, cfg: ArchiveConfig, *, arxiv_version: str | None, etal_suffix: str = "etal"
) -> str:
    """Apply ``naming_template`` and verify against ``SAFE_NAME_RE``."""
    authors = authors_part([a.name for a in record.authors], cfg, etal_suffix=etal_suffix)
    year = year_part(record)
    arxiv = version = ""
    if record.arxiv_id:
        base, own_version = split_arxiv_version(record.arxiv_id)
        arxiv = arxiv_name_part(base)
        version = arxiv_version or own_version or ""
    words = cfg.short_title_words
    while True:
        title = short_title(record.title, max_words=words, max_chars=cfg.short_title_chars)
        name = _render(cfg.naming_template, authors, year, title, arxiv, version) + ".pdf"
        if SAFE_NAME_RE.match(name):
            return name
        if len(name) <= 225 or words <= 1:
            raise ValueError(f"unsafe archive file name: {name!r}")
        words -= 1


_OPTIONAL_RE = re.compile(r"\[([^\[\]]*)\]")


def _render(template: str, authors: str, year: str, title: str, arxiv: str, version: str) -> str:
    values = {
        "authors": authors,
        "year": year,
        "short_title": title,
        "arxiv": arxiv,
        "version": version,
    }

    def _optional(m: re.Match[str]) -> str:
        needed = [n for n in re.findall(r"\{(\w+)\}", m.group(1)) if n != "version"]
        return m.group(1) if all(values.get(n) for n in needed) else ""

    text = _OPTIONAL_RE.sub(_optional, template)
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    if "{" in text or "}" in text:
        raise ValueError(f"unknown placeholder in naming_template: {template!r}")
    return text


def person_folder_name(name: str) -> str:
    """``persons/<Given-Surname>`` folder derived from a name (profile ``folder`` is null)."""
    parts = [alnum(_case_token(t)) for t in collapse_whitespace(nfc(name)).split()]
    return "-".join(p for p in parts if p) or ANONYMOUS
