"""``run / select / count`` commands: the daily/weekly scan and the message selection.

run daily|weekly, select, count. Composes L2 sources, L4 scoring and L5 model/notify.

Library calls go through module attributes (``store.write_json_atomic``, ``topic.score`` ...) so a
lane that has not landed yet can be replaced by a fake in tests. The small context helpers at the
top (:func:`load_context`, :func:`load_profile`, :func:`load_weights`, :func:`emit`) are shared by
``cli_feedback`` and ``cli_notify``.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import json
import sys
import time
from collections.abc import Sequence
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from kallimachos.cli_support import add_command, add_group
from kallimachos.config import load as config_load
from kallimachos.config.schema import Config
from kallimachos.feedback import store as fb_store
from kallimachos.feedback import weights as fb_weights
from kallimachos.http import client as http_client
from kallimachos.interfaces import (
    ApprovalRequired,
    BudgetExhausted,
    Candidate,
    ExitCode,
    FeedbackTag,
    HttpClient,
    KallimachosError,
    Notifier,
    Provider,
    Record,
    Rule,
    RuleContext,
    RunKind,
    RunResult,
    Selection,
    Source,
    SourceStats,
    UsageError,
    VerdictKind,
    Weights,
    Window,
    dump,
)
from kallimachos.model import none as model_none
from kallimachos.model import provider as model_provider
from kallimachos.model import select as model_select
from kallimachos.notify import base as notify_base
from kallimachos.notify import budget as notify_budget
from kallimachos.notify import format as notify_format
from kallimachos.paths import DataLayout
from kallimachos.profile import load as profile_load
from kallimachos.profile.schema import Profile
from kallimachos.records import keys as record_keys
from kallimachos.records import pool as record_pool
from kallimachos.scoring import candidate as scoring_candidate
from kallimachos.scoring import rank as scoring_rank
from kallimachos.scoring import seen as scoring_seen
from kallimachos.scoring import topic as scoring_topic
from kallimachos.scoring import venue_quality
from kallimachos.scoring.rules import base as rules_base
from kallimachos.scoring.rules import core as rules_core
from kallimachos.sources import base as sources_base
from kallimachos.sources import registry as source_registry
from kallimachos.state import lock as state_lock
from kallimachos.state import runs as state_runs
from kallimachos.state import store

FIRST_RUN_DAYS = 7
WINDOW_DAYS = {RunKind.DAILY: 1, RunKind.WEEKLY: 7}
DROP_EXAMPLES = 5


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    g = add_group(sub, "run", help="run the daily or weekly scan", path="run")
    for kind in ("daily", "weekly"):
        p = add_command(g, kind, cmd_run, help=f"{kind} scan", path=f"run {kind}")
        p.add_argument("--date", metavar="YYYY-MM-DD", help="run as if today were this date")
        p.add_argument(
            "--first-run",
            action="store_true",
            help="look back 7 days; writes the seen list",
        )
        p.add_argument("--no-notify", action="store_true", help="do not send the message")
        p.add_argument("--no-model", action="store_true", help="deterministic selection")
        p.add_argument(
            "--rerun", action="store_true", help="recompute an existing run of the same day"
        )
        p.set_defaults(run_kind=kind)
    p = add_command(
        sub,
        "select",
        cmd_select,
        help="record the message selection for a run and mark it shown",
        path="select",
    )
    p.add_argument(
        "--run", required=True, metavar="RUN_ID", help="run id (YYYY-MM-DD-daily|weekly)"
    )
    p.add_argument("--main", required=True, metavar="IDS", help="comma-separated candidate ids")
    p.add_argument("--near", metavar="IDS", help="comma-separated near-miss ids")
    add_command(
        sub, "count", cmd_count, help="UTF-16 length of a message read from stdin", path="count"
    )


# ---------------------------------------------------------------------------
# shared helpers (also used by cli_feedback and cli_notify)
# ---------------------------------------------------------------------------


def flag(args: argparse.Namespace, name: str) -> bool:
    return bool(getattr(args, name, False))


def load_context(args: argparse.Namespace) -> tuple[Config, DataLayout]:
    return config_load.load_config(data_dir=getattr(args, "data_dir", None))


def load_profile(layout: DataLayout) -> Profile:
    """``profile.load.load_profile`` for the data directory (missing file = usage error)."""
    return profile_load.load_profile(layout.profile_file)


def load_weights(layout: DataLayout) -> Weights:
    return fb_weights.load_weights(layout)


def emit(args: argparse.Namespace, data: Any, text: str) -> None:
    """``--json`` prints ``data`` as JSON on stdout, otherwise ``text``."""
    if flag(args, "json"):
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    elif text:
        print(text)


def now_in(config: Config) -> _dt.datetime:
    if config.timezone:
        try:
            return _dt.datetime.now(ZoneInfo(config.timezone))
        except ZoneInfoNotFoundError:
            raise UsageError(f"unknown timezone {config.timezone!r}") from None
    return _dt.datetime.now().astimezone()


def profile_text(profile: Profile) -> str:
    """Plain profile summary handed to the model (no personal data beyond the profile itself)."""
    lines = [f"Field: {profile.field}"] if profile.field else []
    for ch in profile.channels:
        terms = ", ".join(ch.keywords.core[:15])
        lines.append(f"- Channel {ch.id} ({ch.name}, priority {ch.priority}): {terms}")
    if profile.excluded_topics:
        lines.append("Not of interest: " + "; ".join(profile.excluded_topics))
    if profile.notes:
        lines.append(profile.notes.strip())
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def run_window(
    day: _dt.date,
    now: _dt.datetime,
    kind: RunKind,
    first_run: bool,
    *,
    window_days: int | None = None,
) -> Window:
    """Nominal window ending at the run moment (today) or at the end of ``day`` (``--date``);
    sources widen it to their own ``window_days`` where they need to. ``window_days`` overrides
    the length (``schedule run-missed`` merges the missed days into one run)."""
    if day == now.date():
        end = now.astimezone(_dt.UTC)
    else:
        end = _dt.datetime.combine(day + _dt.timedelta(days=1), _dt.time(0), tzinfo=_dt.UTC)
    days = window_days or (FIRST_RUN_DAYS if first_run else WINDOW_DAYS[kind])
    return Window.ending_at(end, days)


def _discover(
    sources: Sequence[Source], window: Window, profile: Profile, http: HttpClient
) -> tuple[record_pool.Pool, dict[str, SourceStats], list[str]]:
    """Run every source through ``sources.base.run_source`` (one source failing never stops the
    run); returns the pool, per-source stats and the sources' warnings."""
    pool = record_pool.Pool()
    stats: dict[str, SourceStats] = {}
    warnings: list[str] = []
    for source in sources:
        result = sources_base.run_source(source, window, profile, http)
        for record in result.records:
            pool.add(record)
        st = stats.setdefault(source.name, SourceStats())
        st.count += result.stats.count
        st.requests += result.stats.requests
        st.errors.extend(e[:300] for e in result.stats.errors)
        if result.stats.budget is not None:
            st.budget = (st.budget or 0.0) + result.stats.budget
        warnings.extend(f"{source.name}: {w}"[:300] for w in result.warnings)
    return pool, stats, warnings


def _prefixed(source_name: str, error: str) -> str:
    """``<source>: <error>`` without doubling a prefix ``run_source`` already added."""
    return error if error.startswith(f"{source_name}:") else f"{source_name}: {error}"


def _seed_counts(record: Record, profile: Profile) -> tuple[int, int]:
    refs = {r.rsplit("/", 1)[-1] for r in record.referenced_works}
    seeds = {s.openalex_id.rsplit("/", 1)[-1] for s in profile.seeds if s.openalex_id}
    found = {f.openalex_id.rsplit("/", 1)[-1] for f in profile.foundational}
    return len(refs & seeds), len(refs & found)


def candidate_from(
    record: Record,
    scores: scoring_topic.ChannelScores,
    profile: Profile,
    config: Config,
) -> Candidate:
    """``scoring.candidate.build_candidate`` (candidate fields, rule trace) plus the message
    formatting: language-aware ``authors_short``, abstract shortening and ``venue_short``."""
    cand = scoring_candidate.build_candidate(
        record,
        scores,
        profile,
        candidate_id=record_keys.candidate_id(record),
        keys=record_keys.keys_for(record),
    )
    authors = [a.name for a in record.authors]
    cand.authors_short = notify_format.authors_short(
        authors,
        lang=config.language,
        etal_suffix="vd." if config.language == "tr" else "et al.",
    )
    abstract = record.abstract or None
    limit = config.ranking.abstract_max_chars
    cand.abstract = notify_format.shorten(abstract, limit) if abstract else None
    cand.abstract_available = bool(abstract)
    cand.abstract_truncated = bool(abstract and len(abstract) > limit)
    cand.venue_short = notify_format.venue_short(cand) or None
    return cand


def core_rules(profile: Profile) -> list[Rule]:
    """The L4 chain (``rules.core.default_rules``): core drop rules, profile rules, certain pass."""
    return rules_core.default_rules(profile)


def _drop_rule(cand: Candidate) -> str:
    for entry in reversed(cand.rule_trace):
        if entry.verdict is VerdictKind.DROP:
            return entry.rule
    return "rules"


def _prior_drops(layout: DataLayout) -> frozenset[str]:
    tags = fb_store.effective_tags(fb_store.load_feedback(layout))
    return frozenset(cid for cid, tag in tags.items() if tag is FeedbackTag.IRRELEVANT)


def _shorten_late_abstracts(cands: list[Candidate], config: Config) -> None:
    cfg = config.ranking
    for cand in cands:
        if cand.abstract and cand.no > cfg.abstract_full_first:
            short = notify_format.shorten(cand.abstract, cfg.abstract_short_chars)
            if short != cand.abstract:
                cand.abstract = short
                cand.abstract_truncated = True


def _existing_failed(layout: DataLayout, rid: str) -> bool:
    """True when the run file of ``rid`` records a failed run (every source errored, nothing
    delivered); such a run never blocks a retry on the same day."""
    try:
        return state_runs.run_failed(state_runs.load_run(layout, rid))
    except KallimachosError:
        return False


def _print_existing(layout: DataLayout, rid: str, args: argparse.Namespace) -> int:
    message_file = layout.message_file(rid)
    if flag(args, "json") or not message_file.exists():
        print(json.dumps(dump(state_runs.load_run(layout, rid)), ensure_ascii=False, indent=2))
    else:
        print(message_file.read_text(encoding="utf-8"))
    print(f"kallimachos: run {rid} exists; use --rerun to recompute it", file=sys.stderr)
    return int(ExitCode.OK)


def _select(
    run: RunResult,
    config: Config,
    profile: Profile,
    args: argparse.Namespace,
    provider: Provider | None,
) -> tuple[Selection, dict[str, str], dict[str, str]]:
    use_model = not flag(args, "no_model") and config.model.provider in ("claude", "codex")
    if not use_model:
        run.selection_rule = "rule: ranking order, certain-pass kept in main"
        return model_none.deterministic_selection(run, config.ranking), {}, {}
    prov = provider or model_provider.get_provider(config.model)
    selection, extras = model_select.model_select(
        run, prov, config, lang=config.language, profile_text=profile_text(profile)
    )
    run.model_fallback = bool(extras.get("model_fallback"))
    run.selection_rule = (
        "rule (model fallback)" if run.model_fallback else f"model ({config.model.provider})"
    )
    run.warnings.extend(str(p) for p in extras.get("problems", [])[:5])
    return selection, dict(extras.get("summaries", {})), dict(extras.get("whys", {}))


def _notify(
    message: notify_format.RenderedMessage,
    notifiers: Sequence[Notifier],
    run: RunResult | None,
) -> tuple[list[str], bool, int]:
    """Send on every channel; returns (errors, approval_missing, delivered_count).

    ``delivered_count`` is the number of channels that accepted the message: only a message that
    actually reached the user marks its candidates as shown in ``seen``."""
    errors: list[str] = []
    approval = False
    delivered = 0
    for notifier in notifiers:
        try:
            result = notifier.send(message)
        except ApprovalRequired as exc:
            approval = True
            errors.append(f"notify {notifier.name}: {exc}")
            continue
        except KallimachosError as exc:
            errors.append(f"notify {notifier.name}: {exc}")
            continue
        if not result.ok:
            errors.append(f"notify {notifier.name}: {result.error or 'failed'}")
            continue
        delivered += 1
    if run is not None:
        run.errors.extend(errors)
    return errors, approval, delivered


def cmd_run(
    args: argparse.Namespace,
    *,
    sources: Sequence[Source] | None = None,
    http: HttpClient | None = None,
    provider: Provider | None = None,
    notifiers: Sequence[Notifier] | None = None,
    now: _dt.datetime | None = None,
) -> int:
    """Discover -> pool -> score -> rules -> rank -> select -> notify; writes runs/<id>.json and
    seen. Exit 5 unapproved, 4 locked, 6 no source answered, 3 partial.

    The keyword parameters inject fakes (tests, integration) and default to the real lanes."""
    started = time.monotonic()
    config, layout = load_context(args)
    dry = flag(args, "dry_run")
    profile = load_profile(layout)
    if not profile.is_approved:
        raise ApprovalRequired(
            "the profile is not approved: show it with `kallimachos profile show`, then "
            "`kallimachos profile approve`"
        )
    kind = RunKind(args.run_kind)
    moment = now or now_in(config)
    day = _dt.date.fromisoformat(args.date) if getattr(args, "date", None) else moment.date()
    rid = state_runs.run_id(kind, day)
    # a failed run (no source answered) is recomputed on the next attempt of the same day
    if (
        layout.run_file(rid).exists()
        and not flag(args, "rerun")
        and not _existing_failed(layout, rid)
    ):
        return _print_existing(layout, rid, args)

    first_run = flag(args, "first_run")
    catch_up = getattr(args, "window_days", None)
    window = run_window(
        day, moment, kind, first_run, window_days=int(catch_up) if catch_up else None
    )
    with contextlib.ExitStack() as stack:
        if not dry:
            lock = state_lock.FileLock(layout.lock_file("run"), owner=rid)
            lock.acquire()
            stack.callback(lock.release)
        weekly = kind is RunKind.WEEKLY
        client = http or http_client.from_config(config, layout, weekly=weekly)
        if sources is not None:
            srcs = list(sources)
        else:
            srcs = source_registry.enabled_sources(
                config, budget=getattr(client, "budget", None), weekly=weekly
            )
        if not srcs:
            raise UsageError("no source is enabled in config.yaml (sources.<name>.enabled)")
        if not dry and any(s.name == "openalex" for s in srcs):
            # run and archive share the OpenAlex ledger: one of them talks to OpenAlex at a time
            oa_lock = state_lock.FileLock(layout.lock_file("openalex"), owner=rid)
            oa_lock.acquire()
            stack.callback(oa_lock.release)
        pool, stats, source_warnings = _discover(srcs, window, profile, client)
        all_failed = all(st.errors and st.count == 0 for st in stats.values())
        if all_failed:
            errors = sorted({e for st in stats.values() for e in st.errors})
            if not dry:
                # the failure leaves a run file (``run_failed`` is true for it): doctor shows
                # "failed" instead of "no runs yet", and schedule run-missed sees the gap
                failed_run = RunResult(
                    run_id=rid,
                    kind=kind,
                    date=day,
                    window=window,
                    sources=stats,
                    errors=[_prefixed(name, e) for name, st in stats.items() for e in st.errors],
                    warnings=list(source_warnings),
                    first_run=first_run,
                    duration_s=round(time.monotonic() - started, 3),
                )
                state_runs.save_run(layout, failed_run)
            if not flag(args, "no_notify") and not dry:
                failed = notify_format.render_failed(rid, "; ".join(errors), config)
                _notify(failed, notifiers or notify_base.get_notifiers(config, layout), None)
            raise BudgetExhausted(f"no source answered for {rid}: {'; '.join(errors)[:300]}")

        weights = load_weights(layout)
        grace = config.rescan.unshown_grace_days
        seen = scoring_seen.SeenIndex.load(layout.seen_file)
        if flag(args, "rerun"):
            # the run is recomputed from scratch: what its earlier attempt recorded (including an
            # attempt whose message never went out) must not block its own candidates
            seen.undo_run(rid)
        seen.expire(today=day, grace_days=grace)
        records: dict[str, Record] = {}
        # Seen filtering happens once, here, before scoring (``SeenIndex.is_blocked``); the
        # chain's SeenRule sees an empty ``seen`` mapping and stays neutral (no double filter).
        ctx = RuleContext(
            run_kind=kind,
            run_date=day,
            profile=profile,
            weights=weights,
            seen={},
            prior_user_drops=_prior_drops(layout),
            extra={
                "records": records,
                "unshown_grace_days": grace,
                "scoring_config": config.scoring,
                "user_quality": venue_quality.load_user_quality(layout.venue_quality_file),
                "paratext_extra": list(config.scoring.paratext_extra),
                "repository_venues_extra": list(config.scoring.repository_venues_extra),
            },
        )
        rules = core_rules(profile)
        dropped: dict[str, int] = {}
        examples: dict[str, list[str]] = {}
        passed: list[Candidate] = []
        for record in pool.records():
            if seen.is_blocked(record, today=day, grace_days=grace):
                dropped["seen"] = dropped.get("seen", 0) + 1
                continue
            seeds, found = _seed_counts(record, profile)
            scores = scoring_topic.score(
                record,
                profile,
                weights,
                config.scoring,
                run_kind=kind,
                seed_citations=seeds,
                foundational_citations=found,
            )
            if not (scores.passes or scores.mandatory):
                dropped["threshold"] = dropped.get("threshold", 0) + 1
                continue
            cand = candidate_from(record, scores, profile, config)
            if cand.id in records:
                continue
            records[cand.id] = record  # the chain's paratext/venue rules read the record
            if cand.always_show_author:
                ctx.exempt = ctx.exempt | {cand.id}
            verdict = rules_base.run_chain(cand, ctx, rules)
            if verdict.kind is VerdictKind.DROP:
                del records[cand.id]
                rule = _drop_rule(cand)
                dropped[rule] = dropped.get(rule, 0) + 1
                ex = examples.setdefault(rule, [])
                if len(ex) < DROP_EXAMPLES:
                    ex.append(cand.id)
                continue
            passed.append(cand)

        ranked, _not_in_main = scoring_rank.rank_and_quota(passed, profile, config.ranking)
        _shorten_late_abstracts(ranked, config)

        run = RunResult(
            run_id=rid,
            kind=kind,
            date=day,
            window=window,
            sources=stats,
            pool_size=len(pool),
            passed=len(passed),
            dropped=dropped,
            dropped_examples=examples,
            candidates=ranked,
            message_budget=config.notify.budget,
            errors=[_prefixed(name, e) for name, st in stats.items() for e in st.errors],
            warnings=list(source_warnings),
            first_run=first_run,
        )
        selection, summaries, whys = _select(run, config, profile, args, provider)
        message = notify_format.render(
            run, selection, config, summaries=summaries, whys=whys, profile=profile
        )
        approval_missing = False
        notify_errors: list[str] = []
        delivered = 0
        silent = config.notify.silent_when_empty and not selection.main
        if not flag(args, "no_notify") and not dry and not silent:
            chosen = notifiers
            if chosen is None:
                chosen = notify_base.get_notifiers(config, layout, approved=flag(args, "yes"))
            notify_errors, approval_missing, delivered = _notify(message, chosen, run)

        # seen = shown: a candidate counts as shown only when at least one channel delivered the
        # message. With --no-notify, an unapproved Markdown file (exit 5) or failing channels the
        # candidates stay unshown and compete again (grace window), instead of silently vanishing.
        shown = {cid: no for no, cid in selection.message_no.items()} if delivered else {}
        for cand in run.candidates:
            no = shown.get(cand.id)
            seen.record_run(
                records[cand.id],
                cand.keys,
                rid,
                shown=no is not None,
                shown_no=int(no) if no is not None and no.isdigit() else None,
            )
        run.duration_s = round(time.monotonic() - started, 3)
        if not dry:
            store.write_text_atomic(layout.message_file(rid), message.text)
            state_runs.save_run(layout, run)
            state_runs.save_selection(layout, selection)
            seen.save()

    emit(args, dump(run), message.text)
    if approval_missing:
        print(
            "kallimachos: a Markdown daily file was not written because it is a user file; "
            "ask the user, then rerun with --yes --rerun",
            file=sys.stderr,
        )
        return int(ExitCode.APPROVAL_REQUIRED)
    if run.errors or notify_errors:
        for err in run.errors:
            print(f"kallimachos: {err}", file=sys.stderr)
        return int(ExitCode.PARTIAL)
    return int(ExitCode.OK)


# ---------------------------------------------------------------------------
# select / count
# ---------------------------------------------------------------------------


def split_ids(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def cmd_select(args: argparse.Namespace) -> int:
    """Validate ids against the run's candidates and write the selection file."""
    config, layout = load_context(args)
    state_runs.parse_run_id(args.run)
    run = state_runs.load_run(layout, args.run)
    selection = model_select.build_selection(
        run, split_ids(args.main), split_ids(args.near), by="model"
    )
    problems = model_select.validate_selection(run, selection, config)
    if problems:
        raise UsageError("selection rejected, nothing written:\n- " + "\n- ".join(problems))
    if not flag(args, "dry_run"):
        # The agent (or Hermes cron, after the agent finished) delivers this message, not a
        # Kallimachos channel: recording the selection is the last step Kallimachos sees, so the
        # numbered candidates are marked shown here. Same lock and entry shape as ``run``;
        # running ``select`` again for the same run leaves a single entry per candidate.
        lock = state_lock.FileLock(layout.lock_file("run"), owner=args.run)
        lock.acquire()
        try:
            state_runs.save_selection(layout, selection)
            _mark_selection_shown(layout, run, selection)
        finally:
            lock.release()
    lines = [f"{no}. {cid}" for no, cid in selection.message_no.items()]
    emit(args, dump(selection), "\n".join(lines))
    return int(ExitCode.OK)


def _mark_selection_shown(layout: DataLayout, run: RunResult, selection: Selection) -> None:
    by_id = {cand.id: cand for cand in run.candidates}
    seen = scoring_seen.SeenIndex.load(layout.seen_file)
    for no, cid in selection.message_no.items():
        cand = by_id[cid]
        seen.mark_shown(
            cand.keys or [cid],
            run.run_id,
            kind=cand.kind,
            title=cand.title,
            shown_no=int(no) if no.isdigit() else None,
        )
    seen.save()


def cmd_count(args: argparse.Namespace) -> int:
    """Print {chars, utf16, limit, over} (limit = ``notify.budget.limit_utf16``)."""
    config, _layout = load_context(args)
    text = sys.stdin.read().rstrip("\n")  # the trailing newline of `echo` is not message text
    result = notify_budget.count(text, limit=config.notify.budget.limit_utf16)
    print(json.dumps(dump(result)))
    return int(ExitCode.OK)
