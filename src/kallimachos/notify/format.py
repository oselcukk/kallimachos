"""Render a run + selection into a message.

One renderer feeds every channel: the plain text (Telegram, e-mail text part) fitted to the UTF-16
budget, an HTML alternative for e-mail and the Markdown daily block with checkboxes. Templates come
from the i18n catalogs; everything else here is language neutral.
"""

from __future__ import annotations

import datetime as _dt
import html
import re
from collections.abc import Mapping
from dataclasses import dataclass

from kallimachos.config.schema import Config
from kallimachos.i18n import load_catalog, t
from kallimachos.interfaces import Candidate, Message, MessageItem, RunKind, RunResult, Selection
from kallimachos.notify import budget as _budget
from kallimachos.notify.budget import (
    SUMMARY_RANGE,
    SUMMARY_SLOT,
    TITLE_SLOT,
    WHY_MAX_CHARS,
    WHY_SLOT,
)
from kallimachos.notify.email_smtp import subject as email_subject
from kallimachos.profile.schema import Profile

_NAME_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}
_TRUNCATION_MARKERS = re.compile(
    r"\s*\[(?:truncated|cut|k\u0131rp\u0131ld\u0131)\]\s*", re.IGNORECASE
)
_WS = re.compile(r"\s+")
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
_URL = re.compile(r"https?://\S+")
_ARXIV_ABS = "https://arxiv.org/abs/{id}"
_DOI_URL = "https://doi.org/{doi}"


@dataclass
class RenderedMessage(Message):
    """A :class:`Message` that also carries the Markdown daily block for the Markdown channel."""

    markdown: str = ""


# ---------------------------------------------------------------------------
# small text helpers
# ---------------------------------------------------------------------------


def clean_text(text: str) -> str:
    return _WS.sub(" ", _TRUNCATION_MARKERS.sub(" ", text)).strip()


def first_sentence(text: str) -> str:
    """The first sentence of ``text`` (whole text when it has no sentence end)."""
    text = clean_text(text)
    m = _SENTENCE_END.search(text)
    return text[: m.end()] if m else text


def shorten(text: str, max_chars: int) -> str:
    """Cut at a sentence boundary when possible; never appends an ellipsis.

    Order: last sentence end within the budget (when it keeps at least a third of it), else the
    last ``; , :`` after half of the budget, else the last word boundary, else a hard cut."""
    text = clean_text(text)
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    window = text[: max_chars + 1]
    ends = [m.end() for m in _SENTENCE_END.finditer(window) if m.end() <= max_chars]
    if ends and ends[-1] >= max_chars // 3:
        return text[: ends[-1]].rstrip()
    clause = max(window.rfind(c, 0, max_chars) for c in ";,:")
    if clause >= max_chars // 2:
        return text[:clause].rstrip()
    space = window.rfind(" ", 0, max_chars + 1)
    if space > 0:
        return text[:space].rstrip(" ,;:-")
    return text[:max_chars]


def date_text(day: _dt.date, lang: str) -> str:
    """``18 Oct 2026`` / ``18 Eki 2026``: ``date.long`` with the ``month.<n>`` catalog names."""
    return t("date.long", lang, day=day.day, month=t(f"month.{day.month}", lang), year=day.year)


def _surname(name: str) -> tuple[str, str]:
    """(initial, surname) of a display name. ``Surname, Given`` and ``Given Surname`` forms;
    ``Jr``/``Sr``/``II``/``III`` stay with the surname."""
    name = clean_text(name)
    if "," in name:
        last, _, given = name.partition(",")
        given_tokens = given.split()
        suffix = ""
        if given_tokens and given_tokens[0].lower() in _NAME_SUFFIXES:
            suffix = " " + given_tokens.pop(0)
        initial = given_tokens[0][0] + "." if given_tokens else ""
        return initial, last.strip() + suffix
    tokens = name.split()
    if not tokens:
        return "", ""
    suffix_tokens: list[str] = []
    while len(tokens) > 1 and tokens[-1].lower().rstrip(",") in _NAME_SUFFIXES:
        suffix_tokens.insert(0, tokens.pop())
    surname = " ".join([tokens[-1], *suffix_tokens])
    initial = tokens[0][0] + "." if len(tokens) > 1 else ""
    return initial, surname


def authors_short(authors: list[str], *, lang: str, etal_suffix: str = "et al.") -> str:
    """``A. Surname, B. Other`` for up to three authors, ``A. Surname et al.`` beyond (``vd.`` for
    Turkish when the default suffix is used)."""
    if not authors:
        return ""
    if lang == "tr" and etal_suffix == "et al.":
        etal_suffix = "vd."

    def one(name: str) -> str:
        initial, surname = _surname(name)
        return f"{initial} {surname}".strip()

    if len(authors) > 3:
        return f"{one(authors[0])} {etal_suffix}"
    return ", ".join(one(a) for a in authors)


def venue_short(cand: Candidate) -> str:
    """Explicit ``venue_short`` wins; arXiv-only records show ``arXiv <id>``; long venues keep their
    first sentence cut at a word boundary within 80 characters."""
    if cand.venue_short:
        return cand.venue_short
    venue = clean_text(cand.venue or "")
    if not venue:
        return f"arXiv {cand.arxiv}" if cand.arxiv else ""
    venue = re.sub(r"^(?:In:\s*|Ed(?:s|ited)?\.?\s+by\s+[^,]+,\s*)", "", venue)
    if len(venue) <= 80:
        return venue
    return shorten(first_sentence(venue), 80)


def candidate_url(cand: Candidate) -> str:
    if cand.url:
        return cand.url
    if cand.doi:
        return _DOI_URL.format(doi=cand.doi)
    if cand.arxiv:
        return _ARXIV_ABS.format(id=cand.arxiv)
    return ""


def escape_markers(text: str) -> str:
    """Neutralise HTML comment delimiters in source text (titles, abstracts, model summaries) so
    that it can never forge or close a ``<!-- kallimachos:... -->`` marker in the Markdown daily
    file or a note."""
    return text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")


def plain_title(title: str) -> str:
    """Readable plain-text title: inline-math dollars and LaTeX braces dropped."""
    text = title.replace("$", "")
    text = re.sub(
        r"\\(?:mathrm|mathbb|mathcal|mathbf|text|emph|textit|textbf)\{([^{}]*)\}", r"\1", text
    )
    return clean_text(text.replace("{", "").replace("}", ""))


# ---------------------------------------------------------------------------
# numbering
# ---------------------------------------------------------------------------


def _no_key(no: str) -> tuple[int, int]:
    if no.startswith("H"):
        return (0, int(no[1:]) if no[1:].isdigit() else 0)
    return (1, int(no) if no.isdigit() else 0)


def numbered_entries(run: RunResult, selection: Selection) -> list[tuple[str, str]]:
    """(message number, candidate id) in message order. Uses ``selection.message_no``; when it is
    empty the numbers are derived from ``main`` then ``near`` (1, 2, ...)."""
    if selection.message_no:
        return sorted(selection.message_no.items(), key=lambda kv: _no_key(kv[0]))
    ids = [*selection.main, *selection.near]
    prefix = "H" if run.kind is RunKind.WEEKLY else ""
    return [(f"{prefix}{i}", cid) for i, cid in enumerate(ids, start=1)]


def _author_tag(cand: Candidate, profile: Profile | None) -> str:
    if not cand.always_show_author or profile is None:
        return ""
    surnames = {_surname(a)[1].casefold() for a in cand.authors}
    for author in profile.authors.always_show:
        for name in (author.name, *author.aliases):
            surname = _surname(name)[1]
            if surname and surname.casefold() in surnames:
                return f"{_surname(author.name)[1]} · "
    return ""


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


@dataclass
class _Entry:
    no: str
    cand: Candidate
    section: str  # "persons" | "main" | "near"


def _entries(run: RunResult, selection: Selection) -> list[_Entry]:
    by_id = {c.id: c for c in run.candidates}
    near = set(selection.near)
    out: list[_Entry] = []
    for no, cid in numbered_entries(run, selection):
        cand = by_id.get(cid)
        if cand is None:
            continue
        if cid in near:
            section = "near"
        elif run.kind is RunKind.WEEKLY and cand.always_show_author:
            section = "persons"
        else:
            section = "main"
        out.append(_Entry(no=no, cand=cand, section=section))
    return out


def _failed_sources(run: RunResult) -> list[str]:
    return sorted(name for name, stats in run.sources.items() if stats.errors)


def resolve_texts(
    run: RunResult,
    selection: Selection,
    config: Config,
    *,
    summaries: Mapping[str, str] | None = None,
    whys: Mapping[str, str] | None = None,
    summary_chars: int = SUMMARY_RANGE[1],
) -> tuple[dict[str, str], dict[str, str]]:
    """Summary and Why per shown main/persons item: model text when given, else the deterministic
    ``none`` text. Both are cleaned and capped (Why at 90 characters)."""
    from kallimachos.model.none import deterministic_summary, deterministic_why

    lang = config.language
    summaries = summaries or {}
    whys = whys or {}
    out_s: dict[str, str] = {}
    out_w: dict[str, str] = {}
    for entry in _entries(run, selection):
        if entry.section == "near":
            continue
        cid = entry.cand.id
        given = summaries.get(cid)
        out_s[cid] = (
            shorten(given, summary_chars)
            if given
            else deterministic_summary(entry.cand, max_chars=summary_chars, lang=lang)
        )
        given_why = whys.get(cid)
        out_w[cid] = shorten(given_why or deterministic_why(entry.cand, lang=lang), WHY_MAX_CHARS)
    return out_s, out_w


def _item_template(
    entry: _Entry, cat: Mapping[str, str], config: Config, profile: Profile | None, lang: str
) -> str:
    cand = entry.cand
    archived = cat["item.archived"] if cand.certain_pass and config.archive.enabled else ""
    text = cat["item"].format(
        no=entry.no,
        archived=archived if entry.section == "main" else "",
        author_tag=_author_tag(cand, profile),
        channels="/".join(cand.channels[:2]),
        title=plain_title(cand.title),
        authors_short=cand.authors_short
        or authors_short(cand.authors, lang=lang, etal_suffix=_etal(config, lang)),
        venue_short=venue_short(cand),
        summary=SUMMARY_SLOT,
        why=WHY_SLOT,
        quality_mark=cand.quality_mark,
        url=candidate_url(cand),
    )
    lines = []
    for line in text.split("\n"):
        line = line.rstrip()
        if line.endswith(" ·"):
            line = line[:-2]
        if WHY_SLOT in line and entry.section == "persons":
            continue  # followed-author items carry no Why line
        lines.append(line)
    return "\n".join(lines).replace("  ", " ").rstrip()


def _etal(config: Config, lang: str) -> str:
    return "vd." if lang == "tr" else "et al."


def _no_summary_line(template: str, cat: Mapping[str, str]) -> str:
    return "\n".join(
        cat["item.no_summary"] if SUMMARY_SLOT in line else line for line in template.split("\n")
    )


def _fixed_text(text: str) -> dict[str, str]:
    return {"kind": "text", "text": text}


def _build_parts(
    run: RunResult,
    selection: Selection,
    config: Config,
    summaries: Mapping[str, str],
    whys: Mapping[str, str],
    profile: Profile | None,
) -> list[dict[str, str]]:
    lang = config.language
    cat = load_catalog(lang)
    entries = _entries(run, selection)
    parts: list[dict[str, str]] = [
        _fixed_text(cat[f"{run.kind.value}.header"].format(date=date_text(run.date, lang)))
    ]
    persons = [e for e in entries if e.section == "persons"]
    main = [e for e in entries if e.section == "main"]
    near = [e for e in entries if e.section == "near"]
    if not persons and not main:
        parts.append(_fixed_text("\n\n" + cat[f"empty.{run.kind.value}"]))
    if persons:
        parts.append(_fixed_text("\n\n" + cat["persons.header"]))
    for entry in [*persons, *main]:
        template = _item_template(entry, cat, config, profile, lang)
        summary = summaries.get(entry.cand.id, "")
        if not summary:
            template = _no_summary_line(template, cat)
        parts.append(
            {
                "kind": "item",
                "template": "\n\n" + template,
                "summary": summary,
                "why": whys.get(entry.cand.id, ""),
            }
        )
    if near:
        prefix, _, suffix = cat["near_miss"].partition("{items}")
        parts.append(_fixed_text("\n\n" + prefix))
        for i, entry in enumerate(near):
            sep = "; " if i < len(near) - 1 else ""
            parts.append(
                {
                    "kind": "near",
                    "template": f"{TITLE_SLOT} ({entry.no}){sep}",
                    "title": plain_title(entry.cand.title),
                }
            )
        if suffix:
            parts.append(_fixed_text(suffix))
    if persons or main or near:
        parts.append(_fixed_text("\n\n" + cat["reply_hint"]))
    failed = _failed_sources(run)
    if failed:
        parts.append(_fixed_text("\n" + cat["source_note"].format(sources=", ".join(failed))))
    elif run.errors:
        parts.append(_fixed_text("\n" + cat["partial"]))
    return parts


def _summary_budget(parts: list[dict[str, str]], config: Config) -> int:
    """Per-item summary budget: (target - fixed) / items with a summary, within [80, 220]."""
    fixed = _budget.utf16_len(
        _budget.assemble([{**p, "summary": ""} if p.get("kind") == "item" else p for p in parts])
    )
    n = sum(1 for p in parts if p.get("kind") == "item" and p.get("summary"))
    if n == 0:
        return SUMMARY_RANGE[1]
    per = (config.notify.budget.target_utf16 - fixed) // n
    return max(SUMMARY_RANGE[0], min(SUMMARY_RANGE[1], per))


def _html(text: str) -> str:
    blocks = []
    for block in text.split("\n\n"):
        lines = []
        for line in block.split("\n"):
            escaped = html.escape(line)
            escaped = _URL.sub(lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', escaped)
            lines.append(escaped)
        blocks.append("<p>" + "<br>\n".join(lines) + "</p>")
    return '<html><body style="font-family: sans-serif">\n' + "\n".join(blocks) + "\n</body></html>"


def render(
    run: RunResult,
    selection: Selection,
    config: Config,
    *,
    summaries: Mapping[str, str] | None = None,
    whys: Mapping[str, str] | None = None,
    profile: Profile | None = None,
) -> RenderedMessage:
    """Plain text within the UTF-16 budget plus HTML for e-mail and the Markdown block. Items are
    never removed to fit; Why, then Summary, then near misses are shortened."""
    s_full, w_full = resolve_texts(run, selection, config, summaries=summaries, whys=whys)
    parts = _build_parts(run, selection, config, s_full, w_full, profile)
    per = _summary_budget(parts, config)
    s_cut = {cid: shorten(text, per) for cid, text in s_full.items()}
    parts = _build_parts(run, selection, config, s_cut, w_full, profile)
    text = _budget.assemble(_budget.fit(parts, config.notify.budget))
    header = load_catalog(config.language)[f"{run.kind.value}.header"].format(
        date=date_text(run.date, config.language)
    )
    return RenderedMessage(
        run_id=run.run_id,
        text=text,
        subject=email_subject(run.run_id, header),
        html=_html(text),
        items=[
            MessageItem(no=no, candidate_id=cid) for no, cid in numbered_entries(run, selection)
        ],
        markdown=render_markdown_block(
            run, selection, config, summaries=s_full, whys=w_full, profile=profile
        ),
    )


def render_failed(run_id: str, error: str, config: Config) -> RenderedMessage:
    """The single ``failed`` line sent when no source answered."""
    cat = load_catalog(config.language)
    text = cat["failed"].format(error=shorten(error, 150))
    return RenderedMessage(
        run_id=run_id,
        text=text,
        subject=email_subject(run_id, text),
        html=_html(text),
        markdown="",
    )


def render_markdown_block(
    run: RunResult,
    selection: Selection,
    config: Config,
    *,
    summaries: Mapping[str, str] | None = None,
    whys: Mapping[str, str] | None = None,
    profile: Profile | None = None,
) -> str:
    """The ``<!-- kallimachos:begin daily run=... -->`` block with checkboxes.

    Markdown has no length limit, so summaries are not shortened to the Telegram budget."""
    from kallimachos.notify.markdown_daily import BEGIN, END, ITEM

    lang = config.language
    cat = load_catalog(lang)
    if summaries is None or whys is None:
        summaries, whys = resolve_texts(run, selection, config, summaries=summaries, whys=whys)
    box = "- [ ] " if config.notify.markdown.checkbox_feedback else "- "
    lines = [
        BEGIN.format(run_id=run.run_id),
        "# " + cat[f"{run.kind.value}.header"].format(date=date_text(run.date, lang)),
    ]
    entries = _entries(run, selection)
    if not entries:
        lines.append(cat[f"empty.{run.kind.value}"])
    for entry in entries:
        cand = entry.cand
        channels = "/".join(cand.channels[:2])
        head = f"{box}{entry.no}. "
        if channels and entry.section != "near":
            head += f"**{channels}** "
        head += escape_markers(plain_title(cand.title))
        who = cand.authors_short or authors_short(
            cand.authors, lang=lang, etal_suffix=_etal(config, lang)
        )
        meta = " · ".join(x for x in (who, venue_short(cand)) if x)
        if meta and entry.section != "near":
            head += f" — {escape_markers(meta)}"
        lines.append(head)
        if entry.section != "near":
            details = [
                escape_markers(summaries.get(cand.id) or cat["item.no_summary"]),
                *([escape_markers(whys[cand.id]) + cand.quality_mark] if whys.get(cand.id) else []),
            ]
            url = candidate_url(cand)
            if url:
                details.append(f"[{cat['item.open']}]({escape_markers(url)})")
            lines.append("  " + " · ".join(details))
        lines.append("  " + ITEM.format(run_id=run.run_id, candidate_id=cand.id))
    lines.append("")
    lines.append(cat["markdown.reply_hint"])
    lines.append(END)
    return "\n".join(lines)
