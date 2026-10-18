"""``feedback`` commands: record and poll feedback, learn weights.

add | parse | poll | apply-weights. Grammar, store and weights live in ``kallimachos.feedback``;
this module wires them. Side effects of a recorded tag: ``read``/``ask`` add the work to the
reading queue (``process.queue``); ``good|read|ask|archive`` are archive triggers, which the
archive lane collects from ``feedback.jsonl`` itself.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kallimachos.cli_run import emit, flag, load_context, load_weights
from kallimachos.cli_support import add_command, add_group
from kallimachos.config.schema import Config
from kallimachos.feedback import grammar
from kallimachos.feedback import store as fb_store
from kallimachos.feedback import weights as fb_weights
from kallimachos.i18n import load_catalog
from kallimachos.interfaces import (
    ApprovalRequired,
    ExitCode,
    FeedbackChannel,
    FeedbackEntry,
    FeedbackIntake,
    FeedbackTag,
    RawFeedback,
    RunResult,
    UsageError,
    Weights,
    dump,
    json_schema,
)
from kallimachos.notify.format import candidate_url, shorten
from kallimachos.paths import DataLayout
from kallimachos.state import runs as state_runs
from kallimachos.state import store

QUEUE_TAGS = (FeedbackTag.READ, FeedbackTag.ASK)
_RUN_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(daily|weekly)\.json$")


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    g = add_group(sub, "feedback", help="record and poll feedback, learn weights", path="feedback")
    p = add_command(g, "add", cmd_add, help="record one feedback item", path="feedback add")
    p.add_argument("--run", required=True, metavar="RUN_ID", help="run the item belongs to")
    p.add_argument("--item", required=True, metavar="N", help="message number (3, H2)")
    p.add_argument(
        "--tag",
        required=True,
        choices=["good", "irrelevant", "read", "ask", "archive"],
        help="verdict: good/irrelevant train weights; read/ask queue it; archive fetches the PDF",
    )
    p.add_argument("--note", metavar="TEXT", help="free note stored with the entry")
    p = add_command(
        g, "parse", cmd_parse, help="parse a reply in the command language", path="feedback parse"
    )
    p.add_argument("text", metavar="TEXT")
    p.add_argument("--run", metavar="RUN_ID", help="run to resolve numbers against")
    p = add_command(
        g, "poll", cmd_poll, help="fetch new replies from a channel", path="feedback poll"
    )
    p.add_argument(
        "--channel",
        choices=["telegram", "imap", "markdown"],
        help="one channel (default: every configured feedback channel)",
    )
    p = add_command(
        g,
        "apply-weights",
        cmd_apply_weights,
        help="propose or apply learned weights",
        path="feedback apply-weights",
    )
    p.add_argument(
        "--auto", action="store_true", help="write without asking (config feedback.weights.mode)"
    )
    p.add_argument("--confirm", action="store_true", help="apply a previously shown proposal")


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------


def tag_label(tag: FeedbackTag, lang: str) -> str:
    words = grammar.SYNONYMS.get(lang, grammar.SYNONYMS["en"]).get(tag)
    return words[0] if words else tag.value


def record_entry(
    layout: DataLayout,
    config: Config,
    run: RunResult,
    entry: FeedbackEntry,
    *,
    dry_run: bool,
) -> tuple[bool, str]:
    """Append one entry (deduplicated by the store) and apply its side effects.

    Returns (written, acknowledgement text in the configured language)."""
    from kallimachos.process import queue as process_queue

    lang = config.language
    cat = load_catalog(lang)
    cand = next((c for c in run.candidates if c.id == entry.candidate_id), None)
    title = shorten(cand.title, 80) if cand else entry.candidate_id
    written = False if dry_run else fb_store.append_feedback(layout, entry)
    extra = ""
    if entry.tag in QUEUE_TAGS:
        extra = cat["feedback.extra.read"]
        if written and cand is not None:
            process_queue.queue_add(
                layout,
                ident=cand.id,
                kind="read" if entry.tag is FeedbackTag.READ else "ask",
                title=cand.title,
                url=candidate_url(cand) or None,
                note=entry.note,
            )
    elif entry.tag is FeedbackTag.ARCHIVE:
        extra = cat["feedback.extra.archive"]
    ack = cat["feedback.ack"].format(
        no=entry.item_no, title=title, tag=tag_label(entry.tag, lang), extra=extra
    )
    return written, ack


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0)


def cmd_add(args: argparse.Namespace) -> int:
    """Resolve the item, append to feedback.jsonl, trigger queue/archive side effects."""
    config, layout = load_context(args)
    state_runs.parse_run_id(args.run)
    run = state_runs.load_run(layout, args.run)
    selection = state_runs.load_selection(layout, args.run)
    cid, via = fb_store.resolve_item(run, selection, args.item)
    entry = FeedbackEntry(
        at=_now(),
        run_id=args.run,
        item_no=args.item,
        candidate_id=cid,
        tag=FeedbackTag(args.tag),
        note=args.note,
        channel=FeedbackChannel.CLI,
        resolved_via=via,  # type: ignore[arg-type]
    )
    written, ack = record_entry(layout, config, run, entry, dry_run=flag(args, "dry_run"))
    text = ack if written or flag(args, "dry_run") else f"{ack} (already recorded)"
    emit(args, {"written": written, "entry": dump(entry), "message": ack}, text)
    return int(ExitCode.OK)


def _parsed_dict(reply: grammar.ParsedReply) -> dict[str, Any]:
    return {
        "items": list(reply.items),
        "tag": reply.tag.value,
        "note": reply.note,
        "ask_target": reply.ask_target,
        "ambiguous": list(reply.ambiguous),
    }


def cmd_parse(args: argparse.Namespace) -> int:
    """Print parsed replies (feedback.grammar) as JSON or text."""
    config, layout = load_context(args)
    replies = grammar.parse(args.text, lang=config.language)
    run = selection = None
    if args.run:
        state_runs.parse_run_id(args.run)
        run = state_runs.load_run(layout, args.run)
        selection = state_runs.load_selection(layout, args.run)
    out: list[dict[str, Any]] = []
    lines: list[str] = []
    for reply in replies:
        data = _parsed_dict(reply)
        if run is not None:
            resolved: dict[str, str] = {}
            for item in reply.items:
                try:
                    resolved[item] = fb_store.resolve_item(run, selection, item)[0]
                except UsageError as exc:
                    data.setdefault("unresolved", {})[item] = str(exc)
            data["resolved"] = resolved
        out.append(data)
        target = f" ({reply.ask_target})" if reply.ask_target else ""
        for item in reply.items:
            cid = data.get("resolved", {}).get(item)
            lines.append(f"{item}{' -> ' + cid if cid else ''}: {reply.tag.value}{target}")
        if reply.ambiguous:
            lines.append(f"ambiguous: {', '.join(reply.ambiguous)}")
    emit(args, out, "\n".join(lines) or "no feedback found")
    return int(ExitCode.OK)


# ---------------------------------------------------------------------------
# poll
# ---------------------------------------------------------------------------


def get_intake(name: str, config: Config, layout: DataLayout) -> FeedbackIntake:
    if name == "telegram":
        from kallimachos.feedback.intake_telegram import TelegramIntake

        return TelegramIntake(config, layout)
    if name == "imap":
        from kallimachos.feedback.intake_imap import ImapIntake

        return ImapIntake(config, layout)
    if name == "markdown":
        from kallimachos.feedback.intake_markdown import MarkdownIntake

        return MarkdownIntake(config, layout)
    raise UsageError(f"unknown feedback channel {name!r}")


def cursor_file(layout: DataLayout, channel: str) -> Path:
    return layout.intake_cursor_file(channel)


def latest_run_id(layout: DataLayout, kind: str, on_or_before: _dt.date) -> str | None:
    """Newest ``runs/<date>-<kind>.json`` not later than the given day (daily and weekly numbers
    never mix)."""
    best: str | None = None
    if not layout.runs.is_dir():
        return None
    for path in layout.runs.iterdir():
        m = _RUN_FILE_RE.match(path.name)
        if not m or m.group(2) != kind or m.group(1) > on_or_before.isoformat():
            continue
        rid = path.name[: -len(".json")]
        if best is None or rid > best:
            best = rid
    return best


@dataclass
class ChannelReport:
    fetched: int = 0
    recorded: int = 0
    duplicates: int = 0
    messages: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    error: str | None = None


def _marker_candidate(raw: RawFeedback) -> str | None:
    if raw.channel is not FeedbackChannel.MARKDOWN or not raw.message_ref:
        return None
    from kallimachos.feedback.intake_markdown import split_ref

    parts = split_ref(raw.message_ref)
    return parts[2] if parts else None


def process_raw(
    raw: RawFeedback,
    layout: DataLayout,
    config: Config,
    report: ChannelReport,
    *,
    dry_run: bool,
) -> None:
    """Parse one reply, resolve run and numbers, record every resolvable item. Anything uncertain
    goes to ``report.unresolved`` so the agent can ask the user one question."""
    for reply in grammar.parse(raw.text, lang=config.language):
        if reply.ambiguous:
            report.unresolved.append(f"{raw.text!r}: ambiguous {', '.join(reply.ambiguous)}")
            continue
        kind = "weekly" if any(i.startswith("H") for i in reply.items) else "daily"
        rid = raw.run_hint or latest_run_id(layout, kind, raw.at.date())
        if rid is None:
            report.unresolved.append(f"{raw.text!r}: no {kind} run to resolve against")
            continue
        try:
            run = state_runs.load_run(layout, rid)
            selection = state_runs.load_selection(layout, rid)
        except UsageError as exc:
            report.unresolved.append(f"{raw.text!r}: {exc}")
            continue
        note = reply.note
        if reply.ask_target:
            note = f"ask:{reply.ask_target}" + (f" {note}" if note else "")
        for item in reply.items:
            try:
                cid, via = fb_store.resolve_item(run, selection, item)
            except UsageError as exc:
                report.unresolved.append(f"{raw.text!r}: {exc}")
                continue
            cid = _marker_candidate(raw) or cid
            entry = FeedbackEntry(
                at=raw.at.astimezone(_dt.UTC).replace(microsecond=0),
                run_id=rid,
                item_no=item,
                candidate_id=cid,
                tag=reply.tag,
                note=note,
                channel=raw.channel,
                resolved_via=via,  # type: ignore[arg-type]
            )
            written, ack = record_entry(layout, config, run, entry, dry_run=dry_run)
            if written:
                report.recorded += 1
                report.messages.append(ack)
            elif not dry_run:
                report.duplicates += 1


def cmd_poll(args: argparse.Namespace) -> int:
    """Run the intake for the channel, parse, record; idempotent via cursor."""
    config, layout = load_context(args)
    dry = flag(args, "dry_run")
    channels = [args.channel] if getattr(args, "channel", None) else list(config.feedback.channels)
    reports: dict[str, ChannelReport] = {}
    for name in channels:
        report = reports[name] = ChannelReport()
        path = cursor_file(layout, name)
        cursor = store.read_json(path, {}) or {}
        try:
            intake = get_intake(name, config, layout)
            raws, new_cursor = intake.poll(cursor)
        except Exception as exc:  # one failing channel does not stop the others
            report.error = f"{type(exc).__name__}: {exc}"[:300]
            continue
        report.fetched = len(raws)
        for raw in raws:
            process_raw(raw, layout, config, report, dry_run=dry)
        if not dry:
            store.write_json_atomic(path, new_cursor)
    data = {name: dump(r) for name, r in reports.items()}
    lines = []
    for name, r in reports.items():
        status = f"error: {r.error}" if r.error else f"{r.recorded} recorded, {r.duplicates} known"
        lines.append(f"{name}: {r.fetched} fetched, {status}")
        lines.extend(f"  {m}" for m in r.messages)
        lines.extend(f"  unresolved {u}" for u in r.unresolved)
    emit(args, data, "\n".join(lines))
    return int(ExitCode.PARTIAL if any(r.error for r in reports.values()) else ExitCode.OK)


# ---------------------------------------------------------------------------
# weights
# ---------------------------------------------------------------------------


def cmd_apply_weights(args: argparse.Namespace) -> int:
    """feedback.weights.propose -> report; write only with --confirm or auto mode."""
    config, layout = load_context(args)
    cfg = config.feedback.weights
    auto = cfg.mode == "auto"
    if flag(args, "auto") and not auto:
        raise ApprovalRequired(
            "--auto needs `feedback.weights.mode: auto` in config.yaml; otherwise show the "
            "report to the user and apply it with --confirm"
        )
    feedback = fb_store.load_feedback(layout)
    runs: dict[str, RunResult] = {}
    for rid in sorted({e.run_id for e in feedback}):
        try:
            runs[rid] = state_runs.load_run(layout, rid)
        except UsageError:
            continue
    weights = load_weights(layout)
    proposals = fb_weights.propose(feedback, runs, weights, cfg)
    report = fb_weights.render_report(proposals, lang=config.language)
    write = bool(proposals) and (flag(args, "confirm") or auto) and not flag(args, "dry_run")
    if write:
        new = fb_weights.apply(weights, proposals, at=_now())
        store.write_json_atomic(layout.weights_file, dump(new), schema=json_schema(Weights))
    data = {"mode": cfg.mode, "written": write, "proposals": [dump(p) for p in proposals]}
    tail = (
        "weights.json updated"
        if write
        else "nothing written; show this to the user and apply with --confirm"
        if proposals
        else "no change proposed"
    )
    emit(args, data, f"{report}\n\n{tail}".strip())
    return int(ExitCode.OK)
