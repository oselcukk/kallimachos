"""Profile from the user's files.

Pipeline: importers -> dedupe -> privacy gate -> match (OpenAlex/Crossref/S2) -> seeds ->
proposals (foundational, channels, core terms, journals, follow authors).

Privacy: paths matching ``archive.sensitive_path_regex`` are never opened; only
strong-id references or >= 6-word titles of article/book/thesis kind are queried; nothing that
looks like a person name or lecture notes leaves the machine. Everything else still feeds the
local proposals (terms, venues) and is listed in the report as "not queried".
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import math
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from kallimachos.config.schema import Config
from kallimachos.interfaces import HttpClient, Record, WorkKind
from kallimachos.profile.importers import (
    ImportedRef,
    bibtex,
    csl_json,
    first_author_surname,
    markdown,
    pdf_meta,
    ris,
)
from kallimachos.profile.journals import propose_journals
from kallimachos.profile.match import Lookup, Match, OpenAlexLookup, match_reference
from kallimachos.profile.schema import (
    Authors,
    Channel,
    CodeSet,
    FollowAuthor,
    Foundational,
    Journal,
    Keywords,
    Profile,
    Seed,
)
from kallimachos.records.normalize import match_key

QUERYABLE_KINDS = frozenset({WorkKind.ARTICLE, WorkKind.BOOK, WorkKind.THESIS})
MIN_TITLE_WORDS = 6
CHANNEL_IDS = "ABCDEFGH"
MAX_CHANNELS = len(CHANNEL_IDS)

_LECTURE_NOTE_RE = re.compile(
    r"\b(lecture|lectures|course|courses|homework|exam|exams|syllabus|slides|handout|"
    r"tutorial|seminar notes|lecture notes|problem set|solutions? manual)\b"
    r"|^(chapter|section|part|week|unit)\s*\d+\b"
    r"|\b(math|cs|phys|stat)\s*\d{2,4}[a-z]?\b",
    re.IGNORECASE,
)
_PERSON_NAME_RE = re.compile(r"^(?:[A-Z][a-z'\-]*\.?\s*){2,4}$")
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "on",
        "in",
        "for",
        "and",
        "or",
        "to",
        "with",
        "by",
        "from",
        "at",
        "as",
        "into",
        "over",
        "under",
        "via",
        "vs",
        "is",
        "are",
        "be",
        "been",
        "being",
        "its",
        "their",
        "this",
        "that",
        "these",
        "those",
        "some",
        "any",
        "all",
        "new",
        "note",
        "notes",
        "towards",
        "toward",
        "about",
        "between",
        "among",
        "through",
        "using",
        "use",
        "based",
        "approach",
        "approaches",
        "results",
        "result",
        "study",
        "studies",
        "analysis",
        "theory",
        "method",
        "methods",
        "problem",
        "problems",
        "i",
        "ii",
        "iii",
        "iv",
        "v",
        "vi",
        "part",
        "one",
        "two",
        "three",
        "first",
        "second",
    }
)
_SUFFIX_IMPORTERS: dict[str, Callable[[Path], list[ImportedRef]]] = {
    ".bib": bibtex.parse,
    ".bibtex": bibtex.parse,
    ".ris": ris.parse,
    ".json": csl_json.parse,
    ".md": markdown.parse,
    ".markdown": markdown.parse,
}


@dataclass
class ProfileDraft:
    profile: Profile
    report_md: str
    skipped_private: list[str] = field(default_factory=list)
    unmatched: list[ImportedRef] = field(default_factory=list)
    not_queried: list[ImportedRef] = field(default_factory=list)
    matches: list[Match] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Collect, dedupe, privacy gate
# ---------------------------------------------------------------------------


def is_sensitive(path: Path, patterns: Sequence[re.Pattern[str]], root: Path | None = None) -> bool:
    """True when the path (relative to ``root`` when given) matches a sensitive pattern."""
    candidates = [path.as_posix()]
    if root is not None:
        with contextlib.suppress(ValueError):
            candidates.append(path.relative_to(root).as_posix())
    return any(p.search(c) for p in patterns for c in candidates)


def collect(
    paths: Sequence[Path],
    *,
    sensitive_patterns: Sequence[re.Pattern[str]],
    pdf_tool: str | None = None,
) -> tuple[list[ImportedRef], list[str]]:
    """Run importers over files/folders; return (refs, skipped sensitive paths).

    Folders are walked here (not by the importers) so that the sensitive-path check happens
    before any file is opened. A sensitive folder is skipped as a whole. The user-given root is
    judged by its own name only (the user pointed at it deliberately; ``/private/tmp`` on macOS
    would otherwise hide everything); files below it are judged by their path relative to it.
    """
    refs: list[ImportedRef] = []
    skipped: list[str] = []
    pdf_extractor = pdf_meta.select_extractor(pdf_tool)
    for given in paths:
        root = given if given.is_dir() else given.parent
        if is_sensitive(Path(given.name), sensitive_patterns):
            skipped.append(str(given))
            continue
        files = sorted(p for p in given.rglob("*") if p.is_file()) if given.is_dir() else [given]
        for file in files:
            relative = file.relative_to(root) if file != given else Path(file.name)
            if is_sensitive(relative, sensitive_patterns):
                skipped.append(str(file))
                continue
            suffix = file.suffix.lower()
            if suffix == ".pdf":
                parsed = pdf_meta.parse(file, tool=_tool_name(pdf_extractor, pdf_tool))
            elif suffix in _SUFFIX_IMPORTERS:
                if suffix in (".md", ".markdown") and file.name.startswith("_"):
                    continue
                parsed = _SUFFIX_IMPORTERS[suffix](file)
            else:
                continue
            refs.extend(parsed)
    return refs, sorted(set(skipped))


def _tool_name(extractor: Callable[[Path], str], requested: str | None) -> str | None:
    if requested:
        return requested
    name = getattr(extractor, "__name__", "")
    return {"_pdftotext": "pdftotext", "_pypdf": "pypdf", "builtin_text": "builtin"}.get(name)


def dedupe(refs: Sequence[ImportedRef]) -> list[ImportedRef]:
    """Merge references with the same ``dedupe_key``; the first occurrence wins, later ones fill
    missing fields and add their source files, tags and project."""
    merged: dict[str, ImportedRef] = {}
    order: list[str] = []
    for ref in refs:
        key = ref.dedupe_key
        if key not in merged:
            merged[key] = ref
            order.append(key)
            continue
        base = merged[key]
        for file in ref.source_files:
            if file not in base.source_files:
                base.extra_source_files.append(file)
        base.year = base.year or ref.year
        base.doi = base.doi or ref.doi
        base.arxiv = base.arxiv or ref.arxiv
        base.venue = base.venue or ref.venue
        base.project = base.project or ref.project
        if base.kind is WorkKind.UNKNOWN:
            base.kind = ref.kind
        if not base.authors:
            base.authors = list(ref.authors)
        for tag in ref.tags:
            if tag not in base.tags:
                base.tags.append(tag)
    return [merged[k] for k in order]


def is_lecture_note_like(title: str) -> bool:
    return bool(_LECTURE_NOTE_RE.search(title))


def is_person_name_like(title: str) -> bool:
    """Two to four capitalised words and nothing else (``Jane Q. Example``)."""
    return bool(_PERSON_NAME_RE.match(title.strip()))


def passes_privacy_gate(ref: ImportedRef) -> bool:
    """Only strong-id refs or >= 6-word titles of article/book/thesis kind may be queried. No
    person-name or lecture-note-like queries. Tested with a request log that must stay empty.

    A DOI/arXiv id is a public literature identifier: only the identifier leaves the machine, so
    such a reference is always allowed. The title rules apply to title searches.
    """
    if ref.doi or ref.arxiv:
        return True
    if is_lecture_note_like(ref.title) or is_person_name_like(ref.title):
        return False
    if ref.kind not in QUERYABLE_KINDS:
        return False
    words = [w for w in match_key(ref.title).split() if w]
    return len(words) >= MIN_TITLE_WORDS


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_profile(
    paths: Sequence[Path],
    config: Config,
    http: HttpClient,
    *,
    lookup: Lookup | None = None,
    pdf_tool: str | None = None,
    field_hint: str = "",
    today: _dt.date | None = None,
) -> ProfileDraft:
    """Files -> draft profile + Markdown report (nothing is written here)."""
    patterns = [re.compile(p) for p in config.archive.sensitive_path_regex]
    refs, skipped = collect(paths, sensitive_patterns=patterns, pdf_tool=pdf_tool)
    refs = dedupe(refs)
    lk = (
        lookup
        if lookup is not None
        else OpenAlexLookup(
            http, s2_enabled=config.sources.semantic_scholar.api_key_ref is not None
        )
    )
    matches: list[Match] = []
    unmatched: list[ImportedRef] = []
    not_queried: list[ImportedRef] = []
    for ref in refs:
        if not passes_privacy_gate(ref):
            not_queried.append(ref)
            continue
        found = match_reference(
            ref,
            http,
            config.profile.match,
            lookup=lk,
            s2_enabled=config.sources.semantic_scholar.api_key_ref is not None,
        )
        if found is None:
            unmatched.append(ref)
        else:
            matches.append(found)
    draft = profile_from_matches(
        matches,
        local_refs=[*unmatched, *not_queried],
        lookup=lk,
        config=config,
        http=http,
        field_hint=field_hint,
        today=today,
    )
    draft.skipped_private = skipped
    draft.unmatched = unmatched
    draft.not_queried = not_queried
    draft.report_md = render_report(draft, source_label="files")
    return draft


def profile_from_matches(
    matches: Sequence[Match],
    *,
    local_refs: Sequence[ImportedRef] = (),
    lookup: Lookup | None,
    config: Config,
    http: HttpClient,
    field_hint: str = "",
    language_hint: str | None = None,
    exclude_author_ids: Iterable[str] = (),
    today: _dt.date | None = None,
) -> ProfileDraft:
    """The proposal step shared by the files and the ORCID/OpenAlex paths."""
    today = today or _dt.date.today()
    warnings: list[str] = []
    channels, assignment = propose_channels(matches, local_refs)
    seeds = _seeds(matches, local_refs, assignment)
    excluded = set(exclude_author_ids)
    follow = _follow_authors(matches, assignment, excluded)
    foundational = _foundational(matches, lookup, config.profile.foundational_max)
    records = [m.record for m in matches]
    source_ids = {
        m.record.venue: m.extra.source_id for m in matches if m.record.venue and m.extra.source_id
    }
    for ref in local_refs:
        if ref.venue:
            records.append(Record(title=ref.title, venue=ref.venue, kind=ref.kind))
    request_log: list[str] = []
    journals = propose_journals(
        records,
        http,
        config.profile,
        source_ids=source_ids,
        year=today.year - 1,
        request_log=request_log,
    )
    _attach_journals(channels, journals, matches, assignment)
    for ch in channels:
        if len(ch.keywords.core) < config.profile.min_core_terms_warning:
            warnings.append(
                f"channel {ch.id} ({ch.name}) has only {len(ch.keywords.core)} core terms; "
                f"add terms before approving"
            )
    if not matches:
        warnings.append("no reference could be matched online; seeds are local-only (low)")
    profile = Profile(
        status="draft",
        language_hint=language_hint or config.language,
        field=field_hint or _field_from_channels(channels),
        channels=channels,
        authors=Authors(follow=follow),
        seeds=seeds,
        foundational=foundational,
        notes=f"Draft generated by `profile build` on {today.isoformat()}; review and approve.",
    )
    requests = list(lookup.requests) if lookup is not None else []
    requests.extend(request_log)
    return ProfileDraft(
        profile=profile,
        report_md="",
        matches=list(matches),
        requests=requests,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Proposal helpers
# ---------------------------------------------------------------------------


def slug(text: str, limit: int = 60) -> str:
    """Folder-safe slug: ``Analytic number theory`` -> ``Analytic-Number-Theory``."""
    words = [w.capitalize() for w in match_key(text).split()]
    out = "-".join(words)[:limit].strip("-.")
    return out or "Channel"


def ngrams(titles: Iterable[str], *, sizes: tuple[int, ...] = (2, 3)) -> Counter[str]:
    """Count 2- and 3-grams across titles (each gram once per title); edge stopwords dropped."""
    counts: Counter[str] = Counter()
    for title in titles:
        tokens = [t for t in match_key(title).split() if not t.isdigit()]
        seen: set[str] = set()
        for n in sizes:
            for i in range(len(tokens) - n + 1):
                gram = tokens[i : i + n]
                if gram[0] in _STOPWORDS or gram[-1] in _STOPWORDS:
                    continue
                if any(len(t) < 2 for t in gram):
                    continue
                text = " ".join(gram)
                if text not in seen:
                    seen.add(text)
                    counts[text] += 1
    return counts


def _terms(titles: Sequence[str], *, core_max: int = 15, broad_max: int = 10) -> Keywords:
    counts = ngrams(titles)
    ranked = [g for g, _ in counts.most_common()]
    repeated = [g for g in ranked if counts[g] >= 2]
    core = repeated[:core_max] if len(repeated) >= 3 else ranked[:core_max]
    # a trigram that contains a kept bigram does not double count; drop the bigram then
    core = _drop_nested(core)
    broad = [g for g in ranked if g not in core][:broad_max]
    return Keywords(core=core, broad=_drop_nested(broad))


def _drop_nested(grams: Sequence[str]) -> list[str]:
    out: list[str] = []
    for gram in grams:
        if any(gram != other and f" {gram} " in f" {other} " for other in grams):
            continue
        out.append(gram)
    return out


def propose_channels(
    matches: Sequence[Match], local_refs: Sequence[ImportedRef] = ()
) -> tuple[list[Channel], dict[int, str]]:
    """Channels from ``project`` tags when the files carry them, else from OpenAlex subfields.

    Returns the channels and an assignment ``id(ref) -> channel id`` for every reference.
    """
    all_refs: list[ImportedRef] = [m.ref for m in matches] + list(local_refs)
    groups: dict[str, list[ImportedRef]] = {}
    projects = Counter(r.project for r in all_refs if r.project)
    if projects:
        for project, _ in projects.most_common(MAX_CHANNELS):
            groups[project] = [r for r in all_refs if r.project == project]
        rest = [r for r in all_refs if not r.project or r.project not in groups]
        if rest and len(groups) < MAX_CHANNELS:
            groups["General"] = rest
    else:
        by_ref: dict[int, list[str]] = {}
        topic_counts: Counter[str] = Counter()
        for m in matches:
            names = m.extra.subfields or m.extra.topics[:1]
            by_ref[id(m.ref)] = names
            topic_counts.update(set(names))
        threshold = max(1, math.ceil(0.1 * len(matches))) if matches else 1
        top = [t for t, c in topic_counts.most_common(MAX_CHANNELS) if c >= threshold]
        if top:
            for topic in top:
                groups[topic] = []
            for ref in all_refs:
                names = by_ref.get(id(ref), [])
                target = next((t for t in top if t in names), None)
                if target is None:
                    continue
                groups[target].append(ref)
            leftover = [r for r in all_refs if not any(r in g for g in groups.values())]
            matched_ids = {id(m.ref) for m in matches}
            if any(id(r) in matched_ids for r in leftover) and len(groups) < MAX_CHANNELS:
                groups["General"] = leftover
        else:
            groups["Imported library"] = all_refs
    channels: list[Channel] = []
    assignment: dict[int, str] = {}
    folders: set[str] = set()
    for index, (name, refs) in enumerate(groups.items()):
        cid = CHANNEL_IDS[index]
        matched_refs = [r for r in refs if any(m.ref is r for m in matches)]
        term_refs = matched_refs if matched_refs else refs
        titles = [_title_of(r, matches) for r in term_refs]
        folder = f"{cid}-{slug(name)}"
        while folder in folders:
            folder += "-x"
        folders.add(folder)
        arxiv_primary = _top_categories(refs, matches)
        channels.append(
            Channel(
                id=cid,
                name=name[:80],
                folder=folder,
                priority=1 if index < 2 else (2 if index < 5 else 3),
                keywords=_terms(titles),
                arxiv=CodeSet(primary=arxiv_primary),
            )
        )
        for ref in refs:
            assignment[id(ref)] = cid
    return channels, assignment


def _title_of(ref: ImportedRef, matches: Sequence[Match]) -> str:
    for m in matches:
        if m.ref is ref and m.record.title:
            return m.record.title
    return ref.title


def _top_categories(refs: Sequence[ImportedRef], matches: Sequence[Match]) -> list[str]:
    counts: Counter[str] = Counter()
    for m in matches:
        if m.ref in refs:
            counts.update(m.record.categories)
    return [c for c, _ in counts.most_common(3)]


def _field_from_channels(channels: Sequence[Channel]) -> str:
    return match_key(channels[0].name).replace(" ", "-") if channels else ""


def _seeds(
    matches: Sequence[Match], local_refs: Sequence[ImportedRef], assignment: dict[int, str]
) -> list[Seed]:
    seeds: list[Seed] = []
    for m in matches:
        record = m.record
        year = record.published.year if record.published else m.ref.year
        first = (
            first_author_surname([record.authors[0].name])
            if record.authors
            else first_author_surname(m.ref.authors) or None
        )
        seeds.append(
            Seed(
                title=record.title or m.ref.title,
                year=year,
                first_author=first,
                doi=record.doi,
                arxiv=record.arxiv_id or m.ref.arxiv,
                openalex_id=record.openalex_id,
                channels=[assignment[id(m.ref)]] if id(m.ref) in assignment else [],
                confidence=m.confidence,
                source_files=m.ref.source_files,
            )
        )
    for ref in local_refs:
        if not (ref.doi or ref.arxiv):
            continue  # title-only unmatched refs are listed in the report, not turned into seeds
        seeds.append(
            Seed(
                title=ref.title,
                year=ref.year,
                first_author=first_author_surname(ref.authors) or None,
                doi=ref.doi,
                arxiv=ref.arxiv,
                channels=[assignment[id(ref)]] if id(ref) in assignment else [],
                confidence="low",
                source_files=ref.source_files,
            )
        )
    return seeds


def _follow_authors(
    matches: Sequence[Match], assignment: dict[int, str], excluded: set[str], *, cap: int = 15
) -> list[FollowAuthor]:
    counts: Counter[str] = Counter()
    info: dict[str, tuple[str | None, str | None]] = {}
    channels: dict[str, Counter[str]] = {}
    for m in matches:
        for author in m.record.authors:
            if (author.openalex_id and author.openalex_id in excluded) or (
                author.orcid and author.orcid in excluded
            ):
                continue
            key = match_key(author.name)
            if not key:
                continue
            counts[key] += 1
            info.setdefault(key, (author.orcid, author.openalex_id))
            if key not in info or info[key] == (None, None):
                info[key] = (author.orcid, author.openalex_id)
            if id(m.ref) in assignment:
                channels.setdefault(key, Counter())[assignment[id(m.ref)]] += 1
    names: dict[str, str] = {}
    for m in matches:
        for author in m.record.authors:
            names.setdefault(match_key(author.name), author.name)
    out: list[FollowAuthor] = []
    for key, count in counts.most_common():
        if count < 2 or len(out) >= cap:
            break
        orcid, oa_id = info[key]
        top_channels = [c for c, _ in channels.get(key, Counter()).most_common(2)]
        out.append(
            FollowAuthor(name=names[key], orcid=orcid, openalex_id=oa_id, channels=top_channels)
        )
    return out


def _foundational(matches: Sequence[Match], lookup: Lookup | None, cap: int) -> list[Foundational]:
    seed_ids = {m.record.openalex_id for m in matches if m.record.openalex_id}
    counts: Counter[str] = Counter()
    for m in matches:
        if m.confidence == "low":
            continue
        for work_id in set(m.record.referenced_works):
            if work_id not in seed_ids:
                counts[work_id] += 1
    ranked = [(w, n) for w, n in counts.most_common() if n >= 2][:cap]
    titles: dict[str, str] = {}
    if ranked and lookup is not None:
        for record, _extra in lookup.works_by_ids([w for w, _ in ranked]):
            if record.openalex_id:
                titles[record.openalex_id] = record.title
    return [
        Foundational(openalex_id=w, title=titles.get(w, ""), cited_by_seeds=n) for w, n in ranked
    ]


def _attach_journals(
    channels: Sequence[Channel],
    journals: Sequence[Journal],
    matches: Sequence[Match],
    assignment: dict[int, str],
) -> None:
    if not channels:
        return
    by_id = {ch.id: ch for ch in channels}
    for journal in journals:
        votes: Counter[str] = Counter()
        jkey = match_key(journal.name)
        for m in matches:
            if m.record.venue and match_key(m.record.venue) == jkey and id(m.ref) in assignment:
                votes[assignment[id(m.ref)]] += 1
        target = votes.most_common(1)[0][0] if votes else channels[0].id
        by_id[target].journals.append(journal)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def render_report(draft: ProfileDraft, *, source_label: str) -> str:
    """Markdown report shown to the user before ``profile approve``."""
    p = draft.profile
    lines: list[str] = [f"# Profile draft from {source_label}", ""]
    lines.append(
        f"Seeds: {len(p.seeds)} (matched online: {len(draft.matches)}) · channels proposed: "
        f"{len(p.channels)} · journals: {sum(len(c.journals) for c in p.channels)} · "
        f"authors to follow: {len(p.authors.follow)} · foundational works: {len(p.foundational)}"
    )
    lines.append("")
    if draft.warnings:
        lines.append("## Warnings")
        lines.extend(f"- {w}" for w in draft.warnings)
        lines.append("")
    lines.append("## Channels (proposal)")
    lines.append("| Id | Name | Priority | Folder | Core terms | Broad terms | Journals |")
    lines.append("|---|---|---|---|---|---|---|")
    for ch in p.channels:
        lines.append(
            f"| {ch.id} | {_cell(ch.name)} | {ch.priority} | {_cell(ch.folder)} | "
            f"{_cell('; '.join(ch.keywords.core))} | {_cell('; '.join(ch.keywords.broad))} | "
            f"{_cell('; '.join(j.name for j in ch.journals))} |"
        )
    lines.append("")
    lines.append("## Seeds")
    lines.append("| Title | Year | First author | Id | Channel | Confidence | Source |")
    lines.append("|---|---|---|---|---|---|---|")
    for s in p.seeds:
        ident = s.doi or (f"arXiv:{s.arxiv}" if s.arxiv else s.openalex_id or "")
        lines.append(
            f"| {_cell(s.title)} | {_cell(s.year)} | {_cell(s.first_author)} | {_cell(ident)} | "
            f"{_cell(','.join(s.channels))} | {s.confidence} | "
            f"{_cell('; '.join(Path(f).name for f in s.source_files))} |"
        )
    lines.append("")
    journals = [j for ch in p.channels for j in ch.journals]
    if journals:
        lines.append("## Journals (proposal)")
        lines.append("| Name | ISSN | OpenAlex | Mode | Yearly volume |")
        lines.append("|---|---|---|---|---|")
        for j in journals:
            lines.append(
                f"| {_cell(j.name)} | {_cell(', '.join(j.issn))} | {_cell(j.openalex_source_id)} | "
                f"{j.mode} | {_cell(j.yearly_volume)} |"
            )
        lines.append("")
    if p.authors.follow:
        lines.append("## Authors to follow (candidates)")
        lines.append("| Name | ORCID | OpenAlex | Channels |")
        lines.append("|---|---|---|---|")
        for a in p.authors.follow:
            lines.append(
                f"| {_cell(a.name)} | {_cell(a.orcid)} | {_cell(a.openalex_id)} | "
                f"{_cell(','.join(a.channels))} |"
            )
        lines.append("")
    if p.foundational:
        lines.append("## Foundational works (cited by >= 2 seeds)")
        lines.append("| OpenAlex | Title | Cited by seeds |")
        lines.append("|---|---|---|")
        for f in p.foundational[:40]:
            lines.append(f"| {f.openalex_id} | {_cell(f.title)} | {f.cited_by_seeds} |")
        if len(p.foundational) > 40:
            lines.append(f"| ... | {len(p.foundational) - 40} more | |")
        lines.append("")
    if draft.unmatched:
        lines.append("## Not matched online")
        for ref in draft.unmatched:
            lines.append(f"- {_cell(ref.title)} ({ref.year or '?'}) — {Path(ref.source_file).name}")
        lines.append("")
    if draft.not_queried:
        lines.append("## Not queried (privacy gate: no strong id, short title or not article-like)")
        for ref in draft.not_queried:
            lines.append(f"- {_cell(ref.title)} — {Path(ref.source_file).name}")
        lines.append("")
    if draft.skipped_private:
        lines.append("## Skipped paths (sensitive_path_regex; never opened)")
        lines.extend(f"- {_cell(path)}" for path in draft.skipped_private)
        lines.append("")
    lines.append(
        f"Requests sent: {len(draft.requests)}. Review, edit `profile.yaml`, then run "
        "`kallimachos profile approve`."
    )
    return "\n".join(lines) + "\n"
