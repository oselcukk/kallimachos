"""Model-assisted selection and its validation.

Hard rules enforced in code: the model only selects and writes, never drops certain-pass
candidates, never writes numbers/scores (message numbers are assigned here, and every digit in a
summary or Why must already occur in the candidate's own title, abstract or venue); invalid output
is retried once, then the ``none`` path is used (``run.model_fallback = true``).

One model call per run: the ``summarize`` and ``why`` instructions are sent together with the
``rerank`` prompt, which carries the profile and candidate data.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from collections.abc import Mapping, Sequence
from importlib import resources
from pathlib import Path
from typing import Any

from kallimachos.config.schema import Config
from kallimachos.interfaces import (
    Candidate,
    KallimachosError,
    Provider,
    RunKind,
    RunResult,
    Selection,
    UsageError,
)

PROMPT_NAMES: tuple[str, ...] = ("rerank", "summarize", "why", "semantic_check")

SELECTION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["main", "near", "items"],
    "properties": {
        "main": {
            "type": "array",
            "items": {"type": "string"},
            "description": "candidate ids for the main list, in message order",
        },
        "near": {
            "type": "array",
            "items": {"type": "string"},
            "description": "candidate ids for near misses (titles only); empty for weekly runs",
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "summary", "why"],
                "properties": {
                    "id": {"type": "string"},
                    "summary": {"type": "string"},
                    "why": {"type": "string"},
                },
            },
            "description": "summary and why for every id in main",
        },
    },
}
"""JSON schema handed to the provider for the selection step (strict: every key required, no
extra keys, so it also satisfies Codex structured output)."""

_PROMPT_COMMENT = re.compile(r"\A\s*<!--.*?-->\s*", re.S)
_DIGITS = re.compile(r"\d+")
_FENCE = re.compile(r"\A\s*```(?:json)?\s*(.*?)\s*```\s*\Z", re.S)


def load_prompt(name: str, prompt_dir: Path | None) -> str:
    """Packaged ``model/prompts/<name>.md`` unless ``prompt_dir`` holds a file of that name. A
    leading HTML comment (authoring note) is dropped."""
    if name not in PROMPT_NAMES:
        raise UsageError(f"unknown prompt {name!r} (expected one of {', '.join(PROMPT_NAMES)})")
    text: str | None = None
    if prompt_dir is not None:
        candidate = Path(prompt_dir).expanduser() / f"{name}.md"
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
    if text is None:
        text = (
            resources.files("kallimachos.model")
            .joinpath("prompts", f"{name}.md")
            .read_text(encoding="utf-8")
        )
    return _PROMPT_COMMENT.sub("", text, count=1)


_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def fill(template: str, values: Mapping[str, str]) -> str:
    """Replace ``{{key}}`` placeholders in one pass; unknown placeholders are left as they are.
    One pass means a placeholder-looking text inside an inserted value (a title containing
    ``{{main_items}}``) is never expanded."""
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template)


# ---------------------------------------------------------------------------
# selection construction and validation
# ---------------------------------------------------------------------------


def _persons(run: RunResult) -> list[str]:
    """Weekly followed-author items: fixed, never selected or removed by the model."""
    if run.kind is not RunKind.WEEKLY:
        return []
    return [c.id for c in sorted(run.candidates, key=lambda c: c.no) if c.always_show_author]


def build_selection(
    run: RunResult, main: Sequence[str], near: Sequence[str], *, by: str
) -> Selection:
    """Construct a selection with ``message_no`` numbering; used by the ``select`` command.

    Daily: main ``1..n``, near misses continue the numbering. Weekly: followed-author items first
    (added when missing), everything numbered ``H1, H2, ...``. Duplicates are kept so that
    :func:`validate_selection` reports them."""
    if by not in ("model", "rule", "user"):
        raise UsageError(f"selection author must be model, rule or user, not {by!r}")
    main_ids = [m.strip() for m in main if m.strip()]
    near_ids = [n.strip() for n in near if n.strip()]
    persons = _persons(run)
    if persons:
        main_ids = persons + [m for m in main_ids if m not in persons]
    prefix = "H" if run.kind is RunKind.WEEKLY else ""
    message_no: dict[str, str] = {}
    for i, cid in enumerate([*main_ids, *near_ids], start=1):
        message_no[f"{prefix}{i}"] = cid
    in_main = set(main_ids)
    return Selection(
        run_id=run.run_id,
        written_at=_dt.datetime.now(_dt.UTC).replace(microsecond=0),
        by=by,  # type: ignore[arg-type]
        main=main_ids,
        near=near_ids,
        quota_not_in_main=[c.id for c in run.candidates if c.quota and c.id not in in_main],
        message_no=message_no,
    )


def _duplicates(ids: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    dup: list[str] = []
    for i in ids:
        if i in seen and i not in dup:
            dup.append(i)
        seen.add(i)
    return dup


def validate_selection(run: RunResult, selection: Selection, config: Config) -> list[str]:
    """Ids must be run candidates, no duplicates, sizes within ``ranking``, certain-pass ids present
    in ``main`` (weekly: followed-author items too). Returns problems (empty = valid)."""
    cfg = config.ranking
    problems: list[str] = []
    known = {c.id: c for c in run.candidates}
    if selection.run_id != run.run_id:
        problems.append(f"selection is for run {selection.run_id}, not {run.run_id}")
    for name, ids in (("main", selection.main), ("near", selection.near)):
        unknown = [i for i in ids if i not in known]
        if unknown:
            problems.append(f"{name}: not candidates of {run.run_id}: {', '.join(unknown)}")
        dup = _duplicates(ids)
        if dup:
            problems.append(f"{name}: duplicate ids: {', '.join(dup)}")
    both = sorted(set(selection.main) & set(selection.near))
    if both:
        problems.append(f"in both main and near: {', '.join(both)}")
    if run.candidates and not selection.main:
        problems.append("main is empty")
    persons = set(_persons(run))
    certain = [c.id for c in run.candidates if c.certain_pass]
    missing = [i for i in certain if i not in selection.main]
    if missing:
        problems.append(f"certain-pass candidates missing from main: {', '.join(missing)}")
    missing_persons = [i for i in sorted(persons) if i not in selection.main]
    if missing_persons:
        problems.append(f"followed-author items missing from main: {', '.join(missing_persons)}")
    selectable = [i for i in selection.main if i not in persons]
    limit = max(cfg.main_items, len(set(certain) - persons))
    if len(selectable) > limit:
        problems.append(f"main has {len(selectable)} items; at most {limit} allowed")
    near_limit = 0 if run.kind is RunKind.WEEKLY else cfg.near_miss
    if len(selection.near) > near_limit:
        problems.append(f"near has {len(selection.near)} items; at most {near_limit} allowed")
    if selection.message_no:
        numbered = list(selection.message_no.values())
        expected = [*selection.main, *selection.near]
        if sorted(numbered) != sorted(expected):
            problems.append("message_no does not number exactly the main and near ids")
    return problems


# ---------------------------------------------------------------------------
# model call
# ---------------------------------------------------------------------------


def _source_text(cand: Candidate) -> str:
    return " ".join(
        x for x in (cand.title, cand.abstract or "", cand.venue or "", cand.venue_short or "") if x
    )


def invented_numbers(text: str, cand: Candidate) -> list[str]:
    """Digit runs in ``text`` that do not occur in the candidate's own title/abstract/venue."""
    allowed = set(_DIGITS.findall(_source_text(cand)))
    return [d for d in _DIGITS.findall(text) if d not in allowed]


def candidates_payload(run: RunResult, config: Config) -> list[dict[str, Any]]:
    """What the model sees per candidate: text and signals, no scores or thresholds."""
    from kallimachos.notify.format import shorten

    cfg = config.ranking
    out: list[dict[str, Any]] = []
    for cand in sorted(run.candidates, key=lambda c: c.no):
        abstract = cand.abstract or ""
        limit = (
            cfg.abstract_max_chars
            if cand.no <= cfg.abstract_full_first
            else cfg.abstract_short_chars
        )
        out.append(
            {
                "id": cand.id,
                "title": cand.title,
                "authors": cand.authors_short or ", ".join(cand.authors[:3]),
                "venue": cand.venue_short or cand.venue or "",
                "date": cand.date.isoformat() if cand.date else "",
                "channels": cand.channel_names or cand.channels,
                "abstract": shorten(abstract, limit) if abstract else "",
                "abstract_available": bool(abstract),
                "signals": list(cand.score_reasons),
                "certain_pass": cand.certain_pass,
                "followed_author": cand.always_show_author,
                "full_journal": cand.full_journal,
                "journal_quota": cand.quota,
                "noise": cand.noise,
                "near_note": cand.near_note.note_ref or cand.near_note.seed
                if cand.near_note
                else "",
                "note": cand.note or "",
            }
        )
    return out


def build_prompt(run: RunResult, config: Config, *, lang: str, profile_text: str) -> str:
    prompt_dir = Path(config.model.prompt_dir) if config.model.prompt_dir else None
    cfg = config.ranking
    values = {
        "language": lang,
        "profile_text": profile_text or "(no profile text)",
        "candidates_json": json.dumps(
            candidates_payload(run, config), ensure_ascii=False, indent=1
        ),
        "main_items": str(cfg.main_items),
        "near_miss": str(cfg.near_miss if run.kind is RunKind.DAILY else 0),
        "run_kind": run.kind.value,
        "summary_max_chars": "220",
        "why_max_chars": "90",
    }
    sections = [fill(load_prompt(name, prompt_dir), values) for name in ("summarize", "why")]
    sections.append(fill(load_prompt("rerank", prompt_dir), values))
    return "\n\n---\n\n".join(s.strip() for s in sections) + "\n"


def parse_model_output(
    raw: str, run: RunResult, config: Config
) -> tuple[Selection, dict[str, str], dict[str, str], list[str]]:
    """JSON text -> (selection, summaries, whys, problems)."""
    from kallimachos.notify.budget import WHY_MAX_CHARS
    from kallimachos.notify.format import shorten

    text = raw.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    empty = build_selection(run, [], [], by="model")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return empty, {}, {}, [f"output is not JSON: {exc.msg}"]
    if not isinstance(data, dict):
        return empty, {}, {}, ["output is not a JSON object"]
    main, near, items = data.get("main"), data.get("near", []), data.get("items", [])
    if not isinstance(main, list) or not all(isinstance(x, str) for x in main):
        return empty, {}, {}, ["'main' must be a list of candidate ids"]
    if not isinstance(near, list) or not all(isinstance(x, str) for x in near):
        return empty, {}, {}, ["'near' must be a list of candidate ids"]
    if not isinstance(items, list):
        return empty, {}, {}, ["'items' must be a list"]
    extra = sorted(set(data) - {"main", "near", "items"})
    selection = build_selection(run, main, near, by="model")
    problems = validate_selection(run, selection, config)
    if extra:
        problems.append(f"unexpected keys: {', '.join(extra)}")
    known = {c.id: c for c in run.candidates}
    summaries: dict[str, str] = {}
    whys: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            problems.append("every item needs an 'id'")
            continue
        cand = known.get(item["id"])
        if cand is None or cand.id not in selection.main:
            continue
        summary = str(item.get("summary") or "").strip()
        why = str(item.get("why") or "").strip()
        bad = invented_numbers(f"{summary} {why}", cand)
        if bad:
            problems.append(f"{cand.id}: numbers not in the source: {', '.join(sorted(set(bad)))}")
            continue
        if summary:
            summaries[cand.id] = summary
        if why:
            whys[cand.id] = shorten(why, WHY_MAX_CHARS)
    return selection, summaries, whys, problems


def model_select(
    run: RunResult,
    provider: Provider,
    config: Config,
    *,
    lang: str,
    profile_text: str = "",
) -> tuple[Selection, dict[str, Any]]:
    """One model call (rerank + summarize + why); invalid output is retried once with the problems
    listed, then the deterministic selection is used.

    Returns ``(selection, extras)`` with ``extras = {"summaries": {id: text}, "whys": {id: text},
    "model_fallback": bool, "problems": [str]}``."""
    from kallimachos.model.none import deterministic_selection

    if not run.candidates:
        return build_selection(run, [], [], by="rule"), {
            "summaries": {},
            "whys": {},
            "model_fallback": False,
            "problems": [],
        }
    prompt = build_prompt(run, config, lang=lang, profile_text=profile_text)
    all_problems: list[str] = []
    problems: list[str] = []
    for attempt in range(2):
        text = prompt
        if attempt:
            text += (
                "\n\nYour previous answer was rejected for these reasons; answer again with valid "
                "JSON only:\n- " + "\n- ".join(problems) + "\n"
            )
        try:
            raw = provider.complete(
                text, timeout_s=config.model.timeout_s, json_schema=SELECTION_OUTPUT_SCHEMA
            )
            selection, summaries, whys, problems = parse_model_output(raw, run, config)
        except KallimachosError as exc:
            problems = [str(exc)]
        if not problems:
            return selection, {
                "summaries": summaries,
                "whys": whys,
                "model_fallback": False,
                "problems": all_problems,
            }
        all_problems.extend(f"attempt {attempt + 1}: {p}" for p in problems)
    return deterministic_selection(run, config.ranking), {
        "summaries": {},
        "whys": {},
        "model_fallback": True,
        "problems": all_problems,
    }
