"""``archive run`` / ``archive retry`` core.

The CLI (``cli_archive``) builds an :class:`ArchiveContext` from config/profile/sources and calls
:func:`run_archive` or :func:`run_retry`. Everything external (HTTP, sources, channel scorer,
filter, clock) is injected so the flow is testable offline.

Permanent rules: only open-access candidates from :mod:`archive.oa`; files are created with
``O_EXCL`` semantics (:func:`download.write_exclusive`) and never overwritten, moved or deleted;
the catalog records every change in ``events[]``.
"""

from __future__ import annotations

import datetime as _dt
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kallimachos.archive import triggers as trig
from kallimachos.archive.catalog import Catalog, UnreachableList, eid_from_sha
from kallimachos.archive.download import (
    NOT_RETRYABLE,
    DownloadError,
    DownloadResult,
    HostPacer,
    download_pdf,
    host_of,
    write_exclusive,
)
from kallimachos.archive.identity import (
    journal_doi,
    normalize_key,
    record_keys,
    split_arxiv_version,
)
from kallimachos.archive.metadata import resolve_metadata
from kallimachos.archive.naming import UNDATED, build_filename, etal_suffix_for, year_part
from kallimachos.archive.oa import EmailResolver, oa_candidates
from kallimachos.archive.rows import (
    ArchiveLogLine,
    ExistingEntry,
    ProcessedEntry,
    append_row,
    read_rows,
)
from kallimachos.archive.taxonomy import MainChannel, folder_for, scored_main_channel
from kallimachos.config.schema import Config
from kallimachos.interfaces import (
    ArchiveTrigger,
    CatalogEntry,
    CatalogEvent,
    CatalogEventType,
    CatalogStatus,
    FeedbackEntry,
    HttpClient,
    OaCandidate,
    Record,
    RetryAttempt,
    RunResult,
    Source,
    UnreachableEntry,
    UsageError,
    Weights,
    WorkKind,
)
from kallimachos.paths import DataLayout
from kallimachos.profile.schema import Profile
from kallimachos.state import store

FilterFn = Callable[[Record, ArchiveTrigger, str | None], str | None]
"""Returns a drop reason, or ``None`` to keep. Exempt triggers never reach it."""

PREPRINT_SOURCES = frozenset({"arxiv", "iacr_eprint", "eprint"})
_KIND_RE = re.compile(r"\b(forbidden|html|not_pdf|too_large|rate_limited|server|network|empty)\b")
UNREACHABLE_MD = "Unreachable.md"


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


@dataclass
class ArchiveContext:
    config: Config
    layout: DataLayout
    profile: Profile
    http: HttpClient
    sources: Mapping[str, Source]
    weights: Weights = field(default_factory=Weights)
    main_channel: MainChannel = scored_main_channel
    keep: FilterFn | None = None
    resolve_email: EmailResolver | None = None
    pacer: HostPacer = field(default_factory=HostPacer)
    now: Callable[[], _dt.datetime] = _utcnow
    hints: dict[str, Record] = field(default_factory=dict)
    lock: AbstractContextManager[Any] | None = None
    feedback: Sequence[FeedbackEntry] | None = None  # None: read state/feedback.jsonl
    runs: Sequence[RunResult] | None = None  # None: read recent runs/*.json

    @property
    def root(self) -> Path:
        if not self.config.archive.root:
            raise UsageError("archive.root is not set in config.yaml")
        return Path(self.config.archive.root).expanduser()


@dataclass
class ArchiveReport:
    triggers: int = 0
    skipped_seen: int = 0
    stored: int = 0
    existing: int = 0
    duplicate: int = 0
    unreachable: int = 0
    filtered: int = 0
    no_metadata: int = 0
    retried: int = 0
    retry_skipped: int = 0
    stored_paths: list[str] = field(default_factory=list)
    superseded: list[tuple[str, str]] = field(default_factory=list)  # (preprint path, journal)
    planned: list[str] = field(default_factory=list)  # dry-run only
    errors: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            k: v for k, v in vars(self).items() if isinstance(v, int) and not isinstance(v, bool)
        } | {"superseded": len(self.superseded)}

    def summary_line(self) -> str:
        """One line for the notification; empty when nothing new happened."""
        if not (self.stored or self.unreachable or self.existing or self.superseded):
            return ""
        parts = [f"archive: {self.stored} stored"]
        if self.existing:
            parts.append(f"{self.existing} already in library")
        if self.unreachable:
            parts.append(f"{self.unreachable} unreachable")
        if self.superseded:
            parts.append(f"{len(self.superseded)} preprints superseded")
        return ", ".join(parts)


@dataclass
class _State:
    catalog: Catalog
    unreachable: UnreachableList
    existing: list[ExistingEntry]
    processed: list[ProcessedEntry]
    new_processed: list[ProcessedEntry] = field(default_factory=list)

    def existing_by_keys(self, keys: Sequence[str]) -> ExistingEntry | None:
        wanted = {normalize_key(k) for k in keys}
        for row in self.existing:
            if wanted.intersection(normalize_key(k) for k in row.keys):
                return row
        return None

    def existing_by_sha(self, sha: str) -> ExistingEntry | None:
        return next((r for r in self.existing if r.sha256 == sha), None)

    def processed_keys(self) -> set[str]:
        out: set[str] = set()
        for row in [*self.processed, *self.new_processed]:
            out.update(normalize_key(k) for k in row.keys)
        return out


def load_state(layout: DataLayout) -> _State:
    return _State(
        catalog=Catalog.load(layout.catalog_file),
        unreachable=UnreachableList.load(layout.unreachable_file),
        existing=read_rows(layout.existing_catalog_file, ExistingEntry),
        processed=read_rows(layout.processed_file, ProcessedEntry),
    )


def seen_keys(state: _State, *, include_processed: bool) -> set[str]:
    """Catalog + open unreachable (+ processed) keys."""
    keys = state.catalog.all_keys() | state.unreachable.open_keys()
    if include_processed:
        keys |= state.processed_keys()
    return keys


def run_archive(
    ctx: ArchiveContext, triggers: Sequence[ArchiveTrigger], *, dry_run: bool = False
) -> ArchiveReport:
    """Process every trigger not already seen, then persist the archive state.

    ``dry_run`` makes no request and writes nothing; it lists what a real run would look up.
    """
    report = ArchiveReport(triggers=len(triggers))
    state = load_state(ctx.layout)
    blocked_all = seen_keys(state, include_processed=True)
    blocked_exempt = seen_keys(state, include_processed=False)
    for trigger in triggers:
        key = normalize_key(trigger.ident)
        blocked = blocked_exempt if trig.is_exempt(trigger) else blocked_all
        if key in blocked:
            report.skipped_seen += 1
            continue
        if dry_run:
            report.planned.append(f"{trig.label(trigger)} {key}")
            continue
        _process(ctx, state, report, trigger)
        blocked_all.add(key)
        blocked_exempt.add(key)
    if not dry_run:
        _persist(ctx, state, report, command="archive run")
    return report


def run_retry(ctx: ArchiveContext, *, dry_run: bool = False) -> ArchiveReport:
    """Retry: open unreachable entries are tried again; entries that failed only with 403 /
    HTML / not-a-PDF / too-large are left alone; 429 hosts are spaced >= 20 s and dropped after
    two consecutive 429 answers (:class:`HostPacer`)."""
    report = ArchiveReport()
    state = load_state(ctx.layout)
    for entry in state.unreachable.open_entries():
        kinds = set(_KIND_RE.findall(entry.reason))
        if kinds and kinds <= NOT_RETRYABLE:
            report.retry_skipped += 1
            continue
        if not entry.keys:
            continue
        if dry_run:
            report.planned.append(f"retry {entry.id}")
            continue
        report.retried += 1
        trigger = ArchiveTrigger(kind="manual", ident=entry.keys[0], note="retry")
        hint = Record(title=entry.title, venue=entry.venue)
        before = report.stored + report.existing + report.duplicate
        ctx.hints.setdefault(normalize_key(entry.keys[0]), hint)
        _process(ctx, state, report, trigger, retry_entry=entry)
        result = "stored" if report.stored + report.existing + report.duplicate > before else "open"
        entry.retries.append(RetryAttempt(at=ctx.now(), result=result))
    if not dry_run:
        _persist(ctx, state, report, command="archive retry")
    return report


def _process(
    ctx: ArchiveContext,
    state: _State,
    report: ArchiveReport,
    trigger: ArchiveTrigger,
    *,
    retry_entry: UnreachableEntry | None = None,
) -> None:
    key = normalize_key(trigger.ident)
    record = resolve_metadata(trigger, ctx.sources, ctx.http, hint=ctx.hints.get(key))
    if record is None or not record.title.strip():
        report.no_metadata += 1
        report.errors.append(f"{key}: no metadata (retried next run)")
        return
    keys = list(dict.fromkeys([key, *record_keys(record)]))
    label = trig.label(trigger)
    match = state.catalog.find_by_keys(keys)
    supersede: CatalogEntry | None = None
    if match is not None:
        if _is_preprint_entry(match) and journal_doi(record):
            supersede = match
        else:
            report.duplicate += 1
            if label not in match.triggers:
                match.triggers.append(label)
            state.unreachable.close(keys, closed_by=f"catalog:{match.id}")
            return
    channel, folder = folder_for(
        record, trigger, ctx.profile, ctx.weights, ctx.config, main_channel=ctx.main_channel
    )
    if not trig.is_exempt(trigger) and ctx.keep is not None and retry_entry is None:
        reason = ctx.keep(record, trigger, channel)
        if reason:
            report.filtered += 1
            state.new_processed.append(
                ProcessedEntry(
                    at=ctx.now(), keys=keys, status="filtered", reason=reason[:500], trigger=label
                )
            )
            return
    existing = state.existing_by_keys(keys)
    if existing is not None and supersede is None:
        _add_existing(ctx, state, report, record, keys, existing, label, channel, folder)
        return
    candidates = oa_candidates(
        record, ctx.sources, ctx.http, ctx.config, resolve_email=ctx.resolve_email
    )
    if supersede is not None:
        candidates = [c for c in candidates if c.source not in PREPRINT_SOURCES]
        if not candidates:
            report.duplicate += 1
            return
    result, failures = _download_first(ctx, candidates)
    if result is None:
        report.unreachable += 1
        _mark_unreachable(ctx, state, record, keys, candidates, failures, folder=folder)
        return
    _store(ctx, state, report, record, keys, result, label, channel, folder, supersede)


def _download_first(
    ctx: ArchiveContext, candidates: Sequence[OaCandidate]
) -> tuple[DownloadResult | None, list[str]]:
    failures: list[str] = []
    tmp_dir = ctx.layout.archive_tmp
    for cand in candidates:
        if not cand.is_pdf:
            continue
        if not ctx.pacer.allowed(cand.url):
            failures.append(f"{_host(cand.url)}: rate_limited host dropped for this run")
            continue
        ctx.pacer.wait(cand.url)
        tmp = tmp_dir / f"{uuid.uuid4().hex}.pdf"
        try:
            result = download_pdf(cand, tmp, ctx.http, max_mb=ctx.config.archive.max_pdf_mb)
        except DownloadError as exc:
            ctx.pacer.record(cand.url, exc.status)
            failures.append(f"{exc.host}: {exc.kind} {exc.message}")
            tmp.unlink(missing_ok=True)
            continue
        ctx.pacer.record(cand.url, 200)
        return result, failures
    if not any(c.is_pdf for c in candidates):
        failures.append("no open-access copy found")
    return None, failures


def _host(url: str) -> str:
    return host_of(url)


def _mark_unreachable(
    ctx: ArchiveContext,
    state: _State,
    record: Record,
    keys: list[str],
    candidates: Sequence[OaCandidate],
    failures: list[str],
    *,
    folder: str | None = None,
) -> None:
    """Record an unreachable work with the folder and file name the archive would have
    used, so a user-supplied copy (bridge ``_Incoming``) lands in the right place."""
    doi = journal_doi(record)
    landing = next((c.url for c in candidates if not c.is_pdf), None) or record.url
    year = year_part(record)
    try:
        filename: str | None = build_filename(
            record, ctx.config.archive, arxiv_version=None, etal_suffix=etal_suffix_for(ctx.config)
        )
    except Exception:  # a name that cannot be built is not a reason to lose the entry
        filename = None
    state.unreachable.upsert(
        UnreachableEntry(
            id=keys[0],
            keys=keys,
            title=record.title,
            authors=[a.name for a in record.authors][:30],
            year=int(year) if year != UNDATED else None,
            doi_url=f"https://doi.org/{doi}" if doi else None,
            landing=landing,
            venue=record.venue,
            folder=folder,
            filename=filename,
            reason="; ".join(failures)[:300] or "no open-access copy found",
            oa_tried=[c.url for c in candidates if c.is_pdf],
        )
    )


def _year(record: Record) -> int | None:
    y = year_part(record)
    return int(y) if y != UNDATED else None


def _kind(record: Record) -> WorkKind:
    if record.kind is not WorkKind.UNKNOWN:
        return record.kind
    if journal_doi(record):
        return WorkKind.ARTICLE
    if record.arxiv_id or record.eprint_id:
        return WorkKind.PREPRINT
    return WorkKind.UNKNOWN


def _is_preprint_entry(entry: CatalogEntry) -> bool:
    return entry.status is CatalogStatus.STORED and (
        entry.kind is WorkKind.PREPRINT or (not entry.doi and bool(entry.arxiv or entry.eprint))
    )


def _base_entry(
    record: Record, keys: list[str], label: str, channel: str | None, folder: str
) -> CatalogEntry:
    arxiv_base, arxiv_version = (
        split_arxiv_version(record.arxiv_id) if record.arxiv_id else (None, None)
    )
    return CatalogEntry(
        id="",
        keys=keys,
        doi=journal_doi(record),
        arxiv=arxiv_base,
        arxiv_version=arxiv_version,
        eprint=record.eprint_id,
        openalex_id=record.openalex_id,
        title=record.title,
        authors=[a.name for a in record.authors][:30],
        year=_year(record),
        venue=record.venue,
        kind=_kind(record),
        channel=channel,
        folder=folder,
        triggers=[label],
    )


def _add_existing(
    ctx: ArchiveContext,
    state: _State,
    report: ArchiveReport,
    record: Record,
    keys: list[str],
    existing: ExistingEntry,
    label: str,
    channel: str | None,
    folder: str,
) -> None:
    entry = _base_entry(record, keys, label, channel, folder)
    entry.id = eid_from_sha(existing.sha256) if existing.sha256 else keys[0]
    if state.catalog.get(entry.id) is not None:
        report.duplicate += 1
        return
    entry.sha256 = existing.sha256
    entry.size = existing.size
    entry.status = CatalogStatus.EXISTING
    entry.events.append(
        CatalogEvent(
            at=ctx.now(),
            type=CatalogEventType.EXISTING,
            detail={"existing_root": existing.root, "existing_path": existing.path},
        )
    )
    state.catalog.add(entry)
    state.unreachable.close(keys, closed_by="existing")
    report.existing += 1


def _store(
    ctx: ArchiveContext,
    state: _State,
    report: ArchiveReport,
    record: Record,
    keys: list[str],
    result: DownloadResult,
    label: str,
    channel: str | None,
    folder: str,
    supersede: CatalogEntry | None,
) -> None:
    same = state.catalog.find_by_sha(result.sha256)
    if same is not None:
        result.path.unlink(missing_ok=True)
        report.duplicate += 1
        if label not in same.triggers:
            same.triggers.append(label)
        state.unreachable.close(keys, closed_by=f"catalog:{same.id}")
        return
    old = state.existing_by_sha(result.sha256)
    if old is not None:
        result.path.unlink(missing_ok=True)
        _add_existing(ctx, state, report, record, keys, old, label, channel, folder)
        return
    root = ctx.root
    version = result.oa.version if result.oa.source == "arxiv" else None
    name = build_filename(
        record,
        ctx.config.archive,
        arxiv_version=version,
        etal_suffix=etal_suffix_for(ctx.config),
    )
    final = write_exclusive(result.path, root / folder / name)
    eid = eid_from_sha(result.sha256)
    if state.catalog.get(eid) is not None:
        raise RuntimeError(f"eid collision for {eid}")
    entry = _base_entry(record, keys, label, channel, folder)
    rel = final.relative_to(root).as_posix()
    now = ctx.now()
    entry.id = eid
    if version:
        entry.arxiv_version = version
    entry.path = rel
    entry.filename = final.name
    entry.sha256 = result.sha256
    entry.size = result.size
    entry.pages = result.pages
    entry.oa_source = result.oa.source
    entry.oa_url = result.oa.url
    entry.downloaded_at = now
    entry.status = CatalogStatus.STORED
    entry.events.append(CatalogEvent(at=now, type=CatalogEventType.STORED, detail={"path": rel}))
    state.catalog.add(entry)
    state.unreachable.close(keys, closed_by="downloaded")
    report.stored += 1
    report.stored_paths.append(rel)
    if supersede is not None:
        state.catalog.add_event(
            supersede.id,
            CatalogEvent(at=now, type=CatalogEventType.SUPERSEDED, detail={"by": eid, "path": rel}),
        )
        report.superseded.append((supersede.path or supersede.id, rel))


def _persist(ctx: ArchiveContext, state: _State, report: ArchiveReport, *, command: str) -> None:
    state.catalog.save()
    state.unreachable.save()
    for row in state.new_processed:
        append_row(ctx.layout.processed_file, row)
    append_row(
        ctx.layout.archive_log_file,
        ArchiveLogLine(
            at=ctx.now(),
            command=command,
            counts=report.counts(),
            errors=[e.rsplit(": ", 1)[0] for e in report.errors][:50],  # ids only, no titles
        ),
    )
    store.write_text_atomic(
        ctx.layout.archive / UNREACHABLE_MD, unreachable_markdown(state.unreachable.entries())
    )
    tmp = ctx.layout.archive_tmp
    if tmp.is_dir() and not any(tmp.iterdir()):
        tmp.rmdir()


def unreachable_markdown(entries: Sequence[UnreachableEntry]) -> str:
    """``Unreachable.md``: open entries for manual download (generated file, data directory)."""
    lines = [
        "<!-- generated by kallimachos archive; edits are overwritten -->",
        "# Unreachable",
        "",
    ]
    open_entries = [e for e in entries if e.status == "open"]
    if not open_entries:
        lines.append("Nothing open.")
    for e in open_entries:
        authors = ", ".join(e.authors[:4]) + (" et al." if len(e.authors) > 4 else "")
        link = e.doi_url or e.landing or ""
        lines.append(f"- {e.title} ({authors}{', ' + str(e.year) if e.year else ''})")
        if link:
            lines.append(f"  - link: {link}")
        lines.append(f"  - reason: {e.reason}")
    return "\n".join(lines) + "\n"


def rule_chain_filter(profile: Profile, weights: Weights) -> FilterFn:
    """Default filter: the scoring rule chain (core drop rules + profile rules) applied
    to a candidate built from the record. Exempt triggers never reach it."""
    from kallimachos.archive.identity import key_of
    from kallimachos.interfaces import Candidate, RuleContext, RunKind, VerdictKind
    from kallimachos.scoring.rules import core
    from kallimachos.scoring.rules.base import run_chain
    from kallimachos.scoring.rules.loader import rules_from_profile

    rules = [
        core.ParatextRule(),
        core.RepositoryVenueRule(),
        core.PredatoryVenueRule(),
        *rules_from_profile(profile),
    ]

    def keep(record: Record, trigger: ArchiveTrigger, channel: str | None) -> str | None:
        keys = record_keys(record)
        cand = Candidate(
            no=0,
            id=key_of(trigger.ident) or trigger.ident,
            title=record.title,
            authors=[a.name for a in record.authors][:15],
            venue=record.venue,
            doi=journal_doi(record),
            arxiv=record.arxiv_id,
            abstract=record.abstract,
            abstract_available=bool(record.abstract),
            channels=[channel] if channel else [],
            keys=keys,
            kind=_kind(record),
        )
        rctx = RuleContext(
            run_kind=RunKind.DAILY,
            run_date=_dt.date.today(),
            profile=profile,
            weights=weights,
        )
        verdict = run_chain(cand, rctx, rules)
        if verdict.kind is VerdictKind.DROP:
            return verdict.reason or "dropped by rule chain"
        return None

    return keep
