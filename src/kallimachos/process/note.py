"""Literature note writing (``process note-write``).

Path:
``<vault>/<literature_folder>/<channel.folder>/<subfolder>/<Surname>-<Year>-<Short-Title>.md``.
Never overwrites: same name without ``--existing`` -> exit 5 with the candidate path. With
``--existing`` only the managed ``full_reading`` block and the Kallimachos frontmatter fields
change.

Kallimachos writes the note itself with file tools (like today's reader agent) and, when Avenox is
installed, runs ``beyin.py sync`` afterwards; it never calls Avenox ``note-create`` for literature
notes (``note-create`` only accepts ``notes/``/``knowledge/`` sources).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kallimachos.config.schema import Config
from kallimachos.i18n import load_catalog
from kallimachos.interfaces import (
    ApprovalRequired,
    Card,
    CatalogEntry,
    NoteFrontmatter,
    PartialFailure,
    UsageError,
    WorkState,
    build,
    dump,
)
from kallimachos.paths import DataLayout
from kallimachos.process import avenox
from kallimachos.process import card as card_mod
from kallimachos.process import links as links_mod
from kallimachos.process import semantic as semantic_mod
from kallimachos.process import status as status_mod
from kallimachos.process.notes_index import (
    NoteRef,
    NotesIndex,
    note_title,
    parse_frontmatter,
    split_frontmatter,
)
from kallimachos.profile.schema import Profile
from kallimachos.records.normalize import match_key, strip_accents
from kallimachos.state import store

MANAGED_BEGIN = "<!-- kallimachos:begin {kind}{attrs} -->"
MANAGED_END = "<!-- kallimachos:end {kind} -->"
REQUIRED_SECTION_KEYS: tuple[str, ...] = (
    "note.section.citation",
    "note.section.summary",
    "note.section.structure",
    "note.section.definitions",
    "note.section.main_results",
    "note.section.method",
    "note.section.open_problems",
    "note.section.prior_work",
    "note.section.caveats",
    "note.section.source_file",
    "note.section.not_found",
)
MANDATORY_SECTION_KEYS: tuple[str, ...] = ("note.section.not_found",)
RELATION_SECTION_KEY = "note.section.relation"
INFERENCE_TAG = "[inference]"
FULL_READING_KIND = "full_reading"
EXISTING_FRONTMATTER_KEYS: tuple[str, ...] = (
    "eid",
    "channel",
    "reader",
    "read_at",
    "source_pdf",
    "zotero",
    "citekey",
    "kallimachos",
    "updated_at",
)
"""The only frontmatter keys ``--existing`` may add or change."""
FRONTMATTER_ORDER: tuple[str, ...] = (
    "kind",
    "project",
    "visibility",
    "updated_at",
    "eid",
    "channel",
    "reader",
    "read_at",
    "source_pdf",
    "zotero",
    "citekey",
    "kallimachos",
)
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_SECTION_NUMBER_RE = re.compile(r"^\d+\.\s*")
_LEADING_ARTICLES = frozenset({"a", "an", "the", "on", "bir"})
_LATEX_FORMAT_RE = re.compile(
    r"\\(?:emph|textbf|textit|textrm|textsc|mathrm|mathbf|mathcal|mathbb|text|operatorname|"
    r"label|cite|ref|left|right|bigl|bigr|,|;|!)\b\*?"
)
_LATEX_CMD_RE = re.compile(r"\\([A-Za-z]+)\*?")
MAX_TITLE_WORDS = 7
MAX_TITLE_CHARS = 60


@dataclass
class NoteWriteResult:
    path: Path
    created: bool
    frontmatter: NoteFrontmatter
    warnings: list[str] = field(default_factory=list)
    sync_result: str | None = None  # Avenox sync outcome or None for plain folders


# ---------------------------------------------------------------------------
# Name and path
# ---------------------------------------------------------------------------


def _ascii(text: str) -> str:
    return "".join(ch for ch in strip_accents(text) if ord(ch) < 128)


def surname_of(name: str) -> str:
    """Surname of ``"First Last"``, ``"Last, First"`` or ``"LAST"``; ASCII letters and hyphens."""
    name = name.strip()
    if not name:
        return ""
    if "," in name:
        surname = name.split(",", 1)[0]
    else:
        tokens = name.split()
        surname = tokens[-1] if tokens else name
        # particles stay with the surname: "van der Waerden" -> "Van-Der-Waerden"
        particles = {"van", "von", "de", "der", "den", "del", "della", "di", "da", "le", "la"}
        i = len(tokens) - 1
        while i - 1 >= 1 and tokens[i - 1].lower() in particles:
            i -= 1
        surname = " ".join(tokens[i:]) if tokens else surname
    words = [w for w in re.split(r"[\s\-]+", _ascii(surname)) if w]
    parts = []
    for w in words:
        cleaned = re.sub(r"[^A-Za-z0-9]", "", w)
        if cleaned:
            # SMITH / smith -> Smith; McDonald keeps its inner capitals
            parts.append(
                cleaned.capitalize() if cleaned.isupper() or cleaned.islower() else cleaned
            )
    return "-".join(parts)


def short_title(title: str) -> str:
    """Hyphenated short title: LaTeX kept as words (formatting commands dropped, ``\\zeta`` ->
    ``Zeta``), leading articles dropped, at most ``MAX_TITLE_WORDS`` words / ``MAX_TITLE_CHARS``
    characters, ASCII only."""
    text = _LATEX_FORMAT_RE.sub(" ", title)
    text = _LATEX_CMD_RE.sub(r" \1 ", text)
    words = [re.sub(r"[^A-Za-z0-9]", "", w) for w in _ascii(text).split()]
    words = [w for w in words if w]
    while len(words) > 1 and words[0].lower() in _LEADING_ARTICLES:
        words = words[1:]
    out: list[str] = []
    for w in words[:MAX_TITLE_WORDS]:
        piece = w if w.isupper() and len(w) <= 4 else w.capitalize()
        candidate = "-".join([*out, piece])
        if len(candidate) > MAX_TITLE_CHARS and out:
            break
        out.append(piece)
    return "-".join(out) or "Untitled"


def note_filename(authors: list[str], year: int | None, title: str, *, etal_suffix: str) -> str:
    """``<Surname>-<Year>-<Short-Title>.md``; 1-3 surnames joined by ``-``, 4+
    ``<First>-<etal>``."""
    surnames = [s for s in (surname_of(a) for a in authors) if s]
    if not surnames:
        who = "Anonymous"
    elif len(surnames) <= 3:
        who = "-".join(surnames)
    else:
        who = f"{surnames[0]}-{etal_suffix}"
    when = str(year) if year else "undated"
    name = f"{who}-{when}-{short_title(title)}"
    name = re.sub(r"-{2,}", "-", name).strip("-")
    if not SAFE_NAME_RE.match(name):
        raise UsageError(f"cannot build a safe note name from {name!r}")
    return name + ".md"


def vault_root(config: Config) -> Path:
    if not config.notes.vault_path:
        raise UsageError("notes.vault_path is not set; run init or edit config.yaml")
    return Path(config.notes.vault_path).expanduser()


def literature_root(config: Config) -> Path:
    return vault_root(config) / config.notes.literature_folder


def note_subfolder(entry: CatalogEntry, profile: Profile) -> Path:
    """``<channel.folder>/<subfolder>``: the first two folders of the archive path (which start
    with the channel folder); falls back to the channel folder alone."""
    parents = Path(entry.path).parent.parts if entry.path else ()
    # ``..`` (or a drive/root) in a catalog row must never move the note out of the literature
    # folder; such a row falls back to the channel folder
    parents = tuple(p for p in parents if p not in ("", ".", "/", "..") and not p.endswith(":"))
    if parents:
        return Path(*parents[:2])
    channel = profile.channel_by_id(entry.channel) if entry.channel else None
    if channel is not None:
        return Path(channel.folder)
    return Path(entry.channel or "uncategorized")


def existing_note_path(existing: Path, config: Config) -> Path:
    """The note ``--existing`` may update: an existing ``.md`` file inside the literature folder
    (relative paths count from the vault root), reached without a symlink component. Anything
    else is a usage error: the agent may be steered by text it read, and the only files
    note-write is allowed to touch are literature notes."""
    from kallimachos.bridge.files import inside_root

    root = vault_root(config)
    lit_root = literature_root(config)
    raw = existing if existing.is_absolute() else root / existing
    path = Path(os.path.abspath(raw))
    if path.suffix.lower() != ".md":
        raise UsageError(f"--existing must name a Markdown note (.md): {path}")
    if not inside_root(path, lit_root):
        raise UsageError(
            f"--existing must point inside the literature folder {lit_root} (no symlink "
            f"components): {path}"
        )
    if not path.is_file():
        raise UsageError(f"--existing note not found: {path}")
    return path


def note_path(entry: CatalogEntry, card: Card, config: Config, profile: Profile) -> Path:
    authors = card.citation.authors or entry.authors
    year = entry.year or card.citation.year
    title = card.citation.title or entry.title
    name = note_filename(authors, year, title, etal_suffix=config.notes.etal_suffix)
    return literature_root(config) / note_subfolder(entry, profile) / name


# ---------------------------------------------------------------------------
# Body validation
# ---------------------------------------------------------------------------


def headings(lang: str) -> dict[str, str]:
    """``note.section.*`` key -> heading text in ``lang`` (English fallback by ``load_catalog``)."""
    catalog = load_catalog(lang)
    keys = (*REQUIRED_SECTION_KEYS, RELATION_SECTION_KEY)
    return {k: catalog[k] for k in keys}


def _section_headings(body: str) -> list[tuple[int, str, int]]:
    """``(level, heading text without numbering, line_no)`` for every heading line (code blocks
    skipped)."""
    out: list[tuple[int, str, int]] = []
    in_code = False
    for no, line in enumerate(body.splitlines(), 1):
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = _HEADING_RE.match(line)
        if m:
            out.append((len(m.group(1)), _SECTION_NUMBER_RE.sub("", m.group(2)), no))
    return out


def _section_text(body: str, heading_key: str) -> str | None:
    """Text of the section whose heading matches ``heading_key`` (``match_key`` form), if any."""
    lines = body.splitlines()
    found = [
        (lvl, no) for lvl, text, no in _section_headings(body) if match_key(text) == heading_key
    ]
    if not found:
        return None
    level, start = found[0]
    out: list[str] = []
    for line in lines[start:]:
        m = _HEADING_RE.match(line)
        if m and len(m.group(1)) <= level:
            break
        out.append(line)
    return "\n".join(out)


def validate_body(body: str, *, lang: str, relation_section: bool | None = None) -> list[str]:
    """No frontmatter (``---`` start is exit 2), H1 required, section headings checked against i18n,
    ``[[`` rule. Returns warnings; raises ``UsageError`` on hard failures.

    ``relation_section``: ``True`` requires every sentence of the "Relation to my work" section to
    carry ``[inference]`` (warning otherwise); ``False`` warns when the section is present.
    """
    if body.lstrip().startswith("---"):
        raise UsageError("note body must not carry frontmatter; note-write generates it")
    if not body.strip():
        raise UsageError("note body is empty")
    if "<!-- kallimachos:" in body:
        raise UsageError(
            "note body must not contain '<!-- kallimachos:' markers; the managed block markers "
            "are written by note-write itself"
        )
    heads = _section_headings(body)
    if not any(lvl == 1 for lvl, _t, _n in heads):
        raise UsageError("note body needs one H1: '# <Authors> (<Year>) - <Full title>'")
    names = headings(lang)
    present = {match_key(text) for lvl, text, _n in heads if lvl >= 2}
    warnings: list[str] = []
    for key in REQUIRED_SECTION_KEYS:
        if match_key(names[key]) not in present:
            msg = f"missing section '{names[key]}'"
            if key in MANDATORY_SECTION_KEYS:
                raise UsageError(msg + " (mandatory; write 'none' when nothing is missing)")
            warnings.append(msg)
    _fixed, fixes = links_mod.fix_fake_links(body)
    if fixes:
        shown = "; ".join(fixes[:5])
        raise UsageError(
            f"fake links in the note body ({len(fixes)}): {shown}. '[[' is only for real links; "
            "write matrices with pmatrix, nested brackets as '[ [', and never '](' adjacent"
        )
    relation = _section_text(body, match_key(names[RELATION_SECTION_KEY]))
    if relation is not None:
        if relation_section is False:
            warnings.append(
                f"section '{names[RELATION_SECTION_KEY]}' present although "
                "notes.personal_relation_section is false"
            )
        untagged = [
            ln.strip()
            for ln in relation.splitlines()[1:]
            if ln.strip()
            and not ln.strip().startswith("#")
            and INFERENCE_TAG not in ln
            and match_key(ln) not in ("no direct relation", "dogrudan iliski yok")
        ]
        if untagged:
            warnings.append(
                f"{len(untagged)} line(s) of '{names[RELATION_SECTION_KEY]}' lack the "
                f"{INFERENCE_TAG} tag"
            )
    elif relation_section is True:
        warnings.append(
            f"section '{names[RELATION_SECTION_KEY]}' missing (write 'no direct relation' "
            "when there is none)"
        )
    return warnings


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------


def _today() -> _dt.date:
    return _dt.datetime.now(tz=_dt.UTC).date()


def build_frontmatter(
    entry: CatalogEntry, card: Card, config: Config, profile: Profile, *, reader: str
) -> NoteFrontmatter:
    today = _today()
    fm_cfg = config.notes.frontmatter
    zotero = f"zotero://select/library/items/{entry.zotero_key}" if entry.zotero_key else None
    return NoteFrontmatter(
        eid=entry.id,
        project=fm_cfg.project or profile.field,
        channel=entry.channel or "",
        reader=reader,
        read_at=today,
        updated_at=today,
        source_pdf=entry.path or "",
        kind=fm_cfg.kind,
        visibility=fm_cfg.visibility,
        zotero=zotero,
        citekey=None,
        extra=dict(fm_cfg.extra),
    )


def yaml_scalar(value: Any) -> str:
    """One YAML scalar: dates ISO, booleans/ints plain, strings quoted when needed."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, _dt.datetime | _dt.date):
        return value.isoformat()
    if value is None:
        return "null"
    text = str(value)
    plain = re.fullmatch(r"[A-Za-z0-9_./:@+-][A-Za-z0-9_./:@+ -]*", text) is not None
    reserved = text.lower() in {"true", "false", "null", "yes", "no", "on", "off", "~"}
    looks_numeric = re.fullmatch(r"[-+]?\d[\d_.]*", text) is not None
    if plain and not reserved and not looks_numeric and ": " not in text and " #" not in text:
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _yaml_value_lines(key: str, value: Any) -> list[str]:
    if isinstance(value, dict):
        lines = [f"{key}:"]
        for k, v in value.items():
            lines.extend("  " + ln for ln in _yaml_value_lines(str(k), v))
        return lines if len(lines) > 1 else [f"{key}: {{}}"]
    if isinstance(value, list):
        if not value:
            return [f"{key}: []"]
        return [f"{key}:", *(f"  - {yaml_scalar(v)}" for v in value)]
    return [f"{key}: {yaml_scalar(value)}"]


def render_frontmatter(fm: NoteFrontmatter) -> str:
    data = dump(fm)
    extra = data.pop("extra", {}) or {}
    lines = ["---"]
    for key in FRONTMATTER_ORDER:
        value = data.get(key)
        if value is None:
            continue
        lines.extend(_yaml_value_lines(key, value))
    for key, value in extra.items():
        if key in FRONTMATTER_ORDER:
            continue  # extra never overrides a note/1 field
        lines.extend(_yaml_value_lines(str(key), value))
    lines.append("---")
    return "\n".join(lines) + "\n"


def update_frontmatter_fields(text: str, fields: dict[str, str]) -> str:
    """Add or update only the given keys in an existing
    frontmatter; other keys unchanged. A file without frontmatter gets one with these keys."""
    fm, body = split_frontmatter(text)
    if fm is None:
        lines = ["---", *(f"{k}: {yaml_scalar(v)}" for k, v in fields.items()), "---"]
        return "\n".join(lines) + "\n" + text
    fm_lines = fm.split("\n")
    if fm_lines and fm_lines[-1] == "":
        fm_lines.pop()
    seen: set[str] = set()
    out: list[str] = []
    i = 0
    while i < len(fm_lines):
        line = fm_lines[i]
        m = re.match(r"^([A-Za-z0-9_-]+):(\s|$)", line)
        key = m.group(1) if m else None
        if key in fields and key not in seen:
            out.append(f"{key}: {yaml_scalar(fields[key])}")
            seen.add(key)
            i += 1
            # drop continuation lines of a block value we replaced
            while i < len(fm_lines) and (
                fm_lines[i].startswith((" ", "\t")) or fm_lines[i].startswith("- ")
            ):
                i += 1
            continue
        out.append(line)
        i += 1
    for key, value in fields.items():
        if key not in seen:
            out.append(f"{key}: {yaml_scalar(value)}")
    return "---\n" + "\n".join(out) + "\n---\n" + body


def frontmatter_fields_for_existing(fm: NoteFrontmatter) -> dict[str, str]:
    """The ``--existing`` field set, string valued."""
    data = dump(fm)
    out: dict[str, str] = {}
    for key in EXISTING_FRONTMATTER_KEYS:
        value = data.get(key)
        if value is None or value == "":
            continue
        out[key] = str(value)
    return out


# ---------------------------------------------------------------------------
# Managed blocks
# ---------------------------------------------------------------------------


def _attrs_text(attrs: dict[str, str]) -> str:
    return "".join(f" {k}={_attr_value(v)}" for k, v in attrs.items())


def _attr_value(value: str) -> str:
    return (
        value
        if re.fullmatch(r"[A-Za-z0-9_.:/@+-]+", value)
        else '"' + value.replace('"', "'") + '"'
    )


def managed_block_re(kind: str) -> re.Pattern[str]:
    begin = re.escape(MANAGED_BEGIN.format(kind=kind, attrs="")).replace(
        re.escape(" -->"), r"(?:\s[^>]*)?\s-->"
    )
    end = re.escape(MANAGED_END.format(kind=kind))
    return re.compile(begin + r".*?" + end, re.DOTALL)


def render_managed_block(kind: str, block: str, attrs: dict[str, str]) -> str:
    begin = MANAGED_BEGIN.format(kind=kind, attrs=_attrs_text(attrs))
    end = MANAGED_END.format(kind=kind)
    return f"{begin}\n{block.strip()}\n{end}"


def upsert_managed_block(text: str, *, kind: str, block: str, attrs: dict[str, str]) -> str:
    """Replace the ``kind`` block in place or append it; idempotent; text outside is untouched."""
    rendered = render_managed_block(kind, block, attrs)
    pattern = managed_block_re(kind)
    if pattern.search(text):
        return pattern.sub(lambda _m: rendered, text, count=1)
    base = text.rstrip("\n")
    sep = "\n\n" if base else ""
    return f"{base}{sep}{rendered}\n"


def remove_managed_block(text: str, kind: str) -> str:
    return managed_block_re(kind).sub("", text)


def demote_headings(body: str, *, drop_h1: bool = True) -> str:
    """``##`` -> ``###`` (one level down) so a body nests inside the ``## Full reading`` heading."""
    out: list[str] = []
    in_code = False
    for line in body.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            out.append(line)
            continue
        m = _HEADING_RE.match(line) if not in_code else None
        if m is None:
            out.append(line)
            continue
        level = len(m.group(1))
        if level == 1 and drop_h1:
            continue
        out.append("#" * min(level + 1, 6) + " " + m.group(2))
    return "\n".join(out).strip("\n")


def full_reading_block(body: str, *, reader: str, date: _dt.date, lang: str) -> str:
    catalog = load_catalog(lang)
    heading = catalog["note.full_reading"].format(reader=reader, date=date.isoformat())
    return f"## {heading}\n\n{demote_headings(body)}"


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def load_catalog_entry(layout: DataLayout, eid: str) -> CatalogEntry | None:
    """The catalog line whose ``id`` is ``eid`` (through the alias map when the PDF changed)."""
    aliases = store.read_json(layout.alias_file, default={}) or {}
    wanted = {eid}
    if isinstance(aliases, dict):
        wanted.update(str(old) for old, new in aliases.items() if new == eid)
    found: CatalogEntry | None = None
    for raw in store.read_jsonl(layout.catalog_file):
        if raw.get("id") in wanted:
            found = build(CatalogEntry, raw)  # last line wins
    return found


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_note(
    layout: DataLayout,
    config: Config,
    profile: Profile,
    eid: str,
    body: str,
    *,
    existing: Path | None = None,
    reader: str | None = None,
    dry_run: bool = False,
) -> NoteWriteResult:
    """Full ``note-write`` pipeline: semantic gate (exit 5) -> validate -> path -> write ->
    notes_index -> Avenox sync -> ``note_written``.

    ``sync_result`` is ``"conflict"``/``"error"`` when Avenox ``sync`` did not succeed; the CLI
    turns that into exit 3 (the note is written either way).
    """
    semantic = semantic_mod.latest_semantic(layout, eid)
    if semantic is None or semantic.result != "pass":
        state = "missing" if semantic is None else semantic.result
        raise ApprovalRequired(
            f"semantic verification for {eid} is {state}; run 'process semantic-record' with a "
            "pass result before note-write"
        )
    card = card_mod.load_card(layout, eid)
    if card is None:
        raise UsageError(f"no card for {eid}; deliver it with 'process card-write' first")
    entry = load_catalog_entry(layout, eid)
    if entry is None:
        raise UsageError(f"{eid} is not in the archive catalog")
    lang = config.notes.language or config.language
    warnings = validate_body(
        body, lang=lang, relation_section=config.notes.personal_relation_section
    )
    reader_name = reader or config.process.models.reader or "agent"
    fm = build_frontmatter(entry, card, config, profile, reader=reader_name)
    root = vault_root(config)
    title = note_title(body, card.citation.title or entry.title)

    if existing is not None:
        path = existing_note_path(existing, config)
        original = path.read_text(encoding="utf-8")
        block = full_reading_block(body, reader=reader_name, date=fm.read_at, lang=lang)
        attrs = {"eid": eid, "reader": reader_name, "date": fm.read_at.isoformat()}
        updated = upsert_managed_block(original, kind=FULL_READING_KIND, block=block, attrs=attrs)
        fields = frontmatter_fields_for_existing(fm)
        # keep zotero/citekey already present in the user's note when we have none
        present, _b = _parse_existing(original)
        for key in ("zotero", "citekey"):
            if key not in fields and present.get(key):
                fields.pop(key, None)
        updated = update_frontmatter_fields(updated, fields)
        created = False
        if not dry_run and updated != original:
            if _sha(path.read_text(encoding="utf-8")) != _sha(original):
                raise PartialFailure(
                    f"{path} changed on disk while note-write ran; nothing written"
                )
            store.write_text_atomic(path, updated)
        text_written = updated
    else:
        path = note_path(entry, card, config, profile)
        if path.exists():
            raise ApprovalRequired(
                f"note exists: {path}. Same work? rerun with --existing {path}; otherwise "
                "rename and retry"
            )
        text_written = render_frontmatter(fm) + "\n" + body.strip("\n") + "\n"
        created = True
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            store.write_text_atomic(path, text_written)

    if dry_run:
        return NoteWriteResult(path=path, created=created, frontmatter=fm, warnings=warnings)

    index = NotesIndex.load(layout.notes_index_file)
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        rel = path.as_posix()
    present_fm, _body = _parse_existing(text_written)
    zotero = present_fm.get("zotero")
    index.set(
        eid,
        NoteRef(
            path=rel,
            title=title,
            updated_at=fm.updated_at.isoformat(),
            zotero_key=str(zotero).rsplit("/", 1)[-1] if zotero else None,
            citekey=str(present_fm["citekey"]) if present_fm.get("citekey") else None,
        ),
    )
    index.save()

    sync_result: str | None = None
    if config.notes.target == "avenox":
        info = avenox.detect(root)
        if info is None:
            warnings.append("notes.target is avenox but beyin.py was not found; sync skipped")
        else:
            result = avenox.sync(info)
            sync_result = str(result.get("status", "error"))
            if sync_result not in ("succeeded", "degraded"):
                warnings.append(
                    f"Avenox sync returned {sync_result}: {result.get('message', '')}".strip()
                )

    status_mod.record_state(layout, eid, WorkState.NOTE_WRITTEN, by="process note-write")
    return NoteWriteResult(
        path=path, created=created, frontmatter=fm, warnings=warnings, sync_result=sync_result
    )


def _parse_existing(text: str) -> tuple[dict[str, Any], str]:
    return parse_frontmatter(text)
