"""``profile`` commands: build, show and approve the research profile.

build | show | approve. Unapproved profile makes `run` exit 5.

``build`` never opens a socket unless ``--live`` is given: without it the recorded fixtures named
by ``KALLIMACHOS_HTTP_FIXTURES`` answer, and a miss simply leaves a reference unmatched.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from kallimachos.cli_support import add_command, add_group
from kallimachos.config import load as config_load
from kallimachos.config.schema import Config
from kallimachos.interfaces import (
    ApprovalRequired,
    ConfigError,
    HttpClient,
    SchemaViolation,
    UsageError,
    build,
    dump,
)
from kallimachos.paths import DataLayout
from kallimachos.profile import from_orcid
from kallimachos.profile import load as profile_load
from kallimachos.profile.from_files import (
    ProfileDraft,
    build_profile,
    profile_from_matches,
    render_report,
)
from kallimachos.profile.match import OpenAlexLookup
from kallimachos.profile.offline import FixtureHttp
from kallimachos.profile.qa import profile_from_answers
from kallimachos.profile.schema import Profile
from kallimachos.profile.wizard import (
    load_answers,
    read_yaml_mapping,
    write_yaml_validated,
)
from kallimachos.records.normalize import match_key
from kallimachos.state import store

REPORT_NAME = "profile-report.md"


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    g = add_group(
        sub, "profile", help="build, inspect and approve the research profile", path="profile"
    )
    p = add_command(
        g,
        "build",
        cmd_build,
        help="build a draft profile from answers, files or ids",
        path="profile build",
    )
    p.add_argument("--qa", metavar="FILE", help="answers.yaml with the Q&A section")
    p.add_argument(
        "--from-files",
        nargs="+",
        metavar="PATH",
        help="PDF folders, .bib, .ris, CSL-JSON, Markdown",
    )
    p.add_argument("--from-orcid", metavar="ORCID", help="ORCID iD")
    p.add_argument("--from-openalex", metavar="AUTHOR_ID", help="OpenAlex author id")
    p.add_argument(
        "--live",
        action="store_true",
        help="make live network requests (default: replay recorded fixtures)",
    )
    p.add_argument(
        "--field", metavar="LABEL", help="free field label for the draft (default: derived)"
    )
    p = add_command(
        g,
        "show",
        cmd_show,
        help="show channels, terms, journals, authors, seeds",
        path="profile show",
    )
    p.add_argument(
        "path", nargs="?", metavar="FILE", help="profile file (default: <data_dir>/profile.yaml)"
    )
    p.add_argument("--table", action="store_true", help="plain-text table (the default output)")
    add_command(
        g,
        "approve",
        cmd_approve,
        help="mark the profile approved (enables `run`)",
        path="profile approve",
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def load_config_and_layout(args: argparse.Namespace) -> tuple[Config, DataLayout]:
    """Config loader (``config.load.load_config``)."""
    return config_load.load_config(None, data_dir=getattr(args, "data_dir", None))


def load_profile(path: Path) -> Profile:
    return profile_load.load_profile(path)


def http_client(args: argparse.Namespace, config: Config, layout: DataLayout) -> HttpClient:
    """``--live``: the shared client (``http.client.from_config``, with the OpenAlex budget);
    otherwise the recorded exchanges named by ``KALLIMACHOS_HTTP_FIXTURES``."""
    if getattr(args, "live", False):
        from kallimachos.http.client import from_config

        return from_config(config, layout)
    return FixtureHttp.from_env()


def render_profile_table(profile: Profile) -> str:
    """The table ``profile show`` prints and the install skill shows to the user before
    approval."""
    lines: list[str] = []
    lines.append(
        f"Profile: field={profile.field or '-'} · status={profile.status} · "
        f"language={profile.language_hint} · channels={len(profile.channels)} · "
        f"seeds={len(profile.seeds)} · foundational={len(profile.foundational)}"
    )
    lines.append("")
    lines.append(
        "| Id | Channel | Pri | Folder | Core | Broad | Exclude | MSC | arXiv | Journals |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for ch in profile.channels:
        lines.append(
            f"| {ch.id} | {_cell(ch.name)} | {ch.priority} | {_cell(ch.folder)} | "
            f"{len(ch.keywords.core)} | {len(ch.keywords.broad)} | {len(ch.negative.exclude)} | "
            f"{_cell(', '.join(ch.msc.primary))} | {_cell(', '.join(ch.arxiv.primary))} | "
            f"{_cell('; '.join(f'{j.name} ({j.mode})' for j in ch.journals))} |"
        )
    lines.append("")
    for ch in profile.channels:
        lines.append(f"{ch.id} core: {_cell('; '.join(ch.keywords.core)) or '-'}")
        if ch.keywords.broad:
            lines.append(f"{ch.id} broad: {_cell('; '.join(ch.keywords.broad))}")
        if ch.negative.exclude or ch.negative.penalty:
            lines.append(
                f"{ch.id} negative: exclude {_cell('; '.join(ch.negative.exclude)) or '-'} · "
                f"penalty {_cell('; '.join(ch.negative.penalty)) or '-'}"
            )
    lines.append("")
    if profile.authors.always_show:
        lines.append(
            "Always show: " + "; ".join(_cell(a.name) for a in profile.authors.always_show)
        )
    if profile.authors.follow:
        lines.append(
            "Follow: "
            + "; ".join(
                f"{_cell(a.name)} [{a.status}{', ' + ','.join(a.channels) if a.channels else ''}]"
                for a in profile.authors.follow
            )
        )
    if profile.seeds:
        lines.append("")
        lines.append("| Seed | Year | First author | Id | Channels | Confidence |")
        lines.append("|---|---|---|---|---|---|")
        for s in profile.seeds:
            ident = s.doi or (f"arXiv:{s.arxiv}" if s.arxiv else s.openalex_id or "")
            lines.append(
                f"| {_cell(s.title)} | {_cell(s.year)} | {_cell(s.first_author)} | "
                f"{_cell(ident)} | {_cell(','.join(s.channels))} | {s.confidence} |"
            )
    if profile.excluded_topics:
        lines.append("")
        lines.append("Excluded topics: " + "; ".join(_cell(t) for t in profile.excluded_topics))
    if profile.notes:
        lines.append("")
        lines.append(f"Notes: {_cell(profile.notes)}")
    return "\n".join(lines) + "\n"


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def merge_profiles(base: Profile, extra: Profile) -> Profile:
    """Union of two drafts: channels re-lettered on id clash, seeds/authors/foundational
    de-duplicated by identifier or normalised title."""
    used = {c.id for c in base.channels}
    renames: dict[str, str] = {}
    for ch in extra.channels:
        new_id = ch.id
        if new_id in used:
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                if letter not in used:
                    new_id = letter
                    break
            renames[ch.id] = new_id
            ch.id = new_id
            ch.folder = f"{new_id}-{ch.folder.split('-', 1)[-1]}" if "-" in ch.folder else ch.folder
        used.add(new_id)
        base.channels.append(ch)

    def remap(ids: list[str]) -> list[str]:
        return [renames.get(i, i) for i in ids]

    seed_keys = {_seed_key(s) for s in base.seeds}
    for seed in extra.seeds:
        key = _seed_key(seed)
        if key in seed_keys:
            continue
        seed.channels = remap(seed.channels)
        base.seeds.append(seed)
        seed_keys.add(key)
    follow_keys = {match_key(a.name) for a in base.authors.follow}
    for author in extra.authors.follow:
        if match_key(author.name) in follow_keys:
            continue
        author.channels = remap(author.channels)
        base.authors.follow.append(author)
        follow_keys.add(match_key(author.name))
    always_keys = {match_key(a.name) for a in base.authors.always_show}
    base.authors.always_show.extend(
        a for a in extra.authors.always_show if match_key(a.name) not in always_keys
    )
    found_ids = {f.openalex_id for f in base.foundational}
    base.foundational.extend(f for f in extra.foundational if f.openalex_id not in found_ids)
    for topic in extra.excluded_topics:
        if topic not in base.excluded_topics:
            base.excluded_topics.append(topic)
    if not base.field:
        base.field = extra.field
    return base


def _seed_key(seed: Any) -> str:
    return (
        seed.doi
        or (f"arxiv:{seed.arxiv}" if seed.arxiv else None)
        or (seed.openalex_id or match_key(seed.title)[:60])
    )


def _qa_section(path: Path) -> dict[str, Any]:
    """``--qa FILE``: a full answers.yaml (``profile.qa``) or a bare Q&A mapping."""
    raw = read_yaml_mapping(path)
    if "profile" in raw and isinstance(raw["profile"], dict) and "qa" in raw["profile"]:
        answers = load_answers(path)
        qa = answers["profile"]["qa"]
        if not isinstance(qa, dict):
            raise UsageError(f"{path}: profile.qa is empty")
        return qa
    return raw


def cmd_build(args: argparse.Namespace) -> int:
    """Merge the selected sources into profile.yaml (draft) + profile-report.md."""
    if not (args.qa or args.from_files or args.from_orcid or args.from_openalex):
        raise UsageError("give at least one of --qa, --from-files, --from-orcid, --from-openalex")
    config, layout = load_config_and_layout(args)
    profile: Profile | None = None
    reports: list[str] = []
    warnings: list[str] = []
    requests = 0
    field_hint = args.field or ""

    if args.qa:
        qa_profile, qa_warnings = profile_from_answers(
            _qa_section(Path(args.qa)),
            language_hint=config.language,
            min_core_terms=config.profile.min_core_terms_warning,
        )
        warnings.extend(qa_warnings)
        profile = qa_profile
        reports.append("# Profile draft from answers\n\n" + render_profile_table(qa_profile))

    http: HttpClient | None = None
    if args.from_files or args.from_orcid or args.from_openalex:
        http = http_client(args, config, layout)

    if args.from_files:
        assert http is not None
        paths = [Path(p).expanduser() for p in args.from_files]
        missing = [str(p) for p in paths if not p.exists()]
        if missing:
            raise UsageError(f"path not found: {', '.join(missing)}")
        draft = build_profile(paths, config, http, field_hint=field_hint)
        profile = draft.profile if profile is None else merge_profiles(profile, draft.profile)
        reports.append(draft.report_md)
        warnings.extend(draft.warnings)
        requests += len(draft.requests)

    if args.from_orcid or args.from_openalex:
        assert http is not None
        draft = _person_draft(args, config, http, field_hint)
        profile = draft.profile if profile is None else merge_profiles(profile, draft.profile)
        reports.append(draft.report_md)
        warnings.extend(draft.warnings)
        requests += len(draft.requests)

    assert profile is not None
    profile.status = "draft"
    profile.approved_at = None
    if field_hint:
        profile.field = field_hint
    report_md = "\n".join(reports)

    if layout.profile_file.exists() and not args.dry_run:
        existing = load_profile(layout.profile_file)
        if existing.is_approved and not args.yes:
            raise ApprovalRequired(
                f"{layout.profile_file} is approved; re-run with --yes to replace it with a draft"
            )
    written: list[str] = []
    if not args.dry_run:
        write_yaml_validated(layout.profile_file, dump(profile), "profile.schema.json")
        written.append(str(layout.profile_file))
        report_path = layout.root / REPORT_NAME
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_md, encoding="utf-8")
        written.append(str(report_path))

    if args.json:
        print(
            json.dumps(
                {
                    "written": written,
                    "status": profile.status,
                    "channels": len(profile.channels),
                    "seeds": len(profile.seeds),
                    "journals": sum(len(c.journals) for c in profile.channels),
                    "follow_authors": len(profile.authors.follow),
                    "foundational": len(profile.foundational),
                    "requests": requests,
                    "warnings": warnings,
                },
                ensure_ascii=False,
            )
        )
    else:
        sys.stdout.write(report_md)
        for path in written:
            print(f"wrote {path}")
        if args.dry_run:
            print("dry run: nothing was written")
    return 0


def _person_draft(
    args: argparse.Namespace, config: Config, http: HttpClient, field_hint: str
) -> ProfileDraft:
    lookup = OpenAlexLookup(http)
    exclude: list[str] = []
    request_log: list[str] = []
    if args.from_orcid:
        orcid = from_orcid.normalize_orcid(args.from_orcid)
        records = from_orcid.works_from_orcid(orcid, config, http, request_log=request_log)
        exclude.append(orcid)
        matches, unmatched = from_orcid.match_person_works(
            records, config, http, lookup=lookup, source_label=f"orcid:{orcid}", via_hint="orcid"
        )
        label = f"ORCID {orcid}"
    else:
        author_id = from_orcid.normalize_openalex_author(args.from_openalex)
        pairs = lookup.works_by_author(author_id)
        exclude.append(author_id)
        matches = from_orcid.matches_from_openalex_records(pairs, f"openalex:{author_id}")
        unmatched = []
        label = f"OpenAlex author {author_id}"
    draft = profile_from_matches(
        matches,
        local_refs=unmatched,
        lookup=lookup,
        config=config,
        http=http,
        field_hint=field_hint,
        exclude_author_ids=exclude,
    )
    draft.unmatched = unmatched
    draft.requests = [*request_log, *draft.requests]
    if not matches and not unmatched:
        draft.warnings.append(f"{label}: no works found (offline without fixtures?)")
    draft.report_md = render_report(draft, source_label=label)
    return draft


# ---------------------------------------------------------------------------
# show / approve
# ---------------------------------------------------------------------------


def cmd_show(args: argparse.Namespace) -> int:
    """Validate and print the profile table."""
    if args.path:
        path = Path(args.path).expanduser()
    else:
        _config, layout = load_config_and_layout(args)
        path = layout.profile_file
    profile = load_profile(path)
    if args.json:
        print(json.dumps(dump(profile), ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(render_profile_table(profile))
    return 0


def approve_in_place(path: Path, profile: Profile) -> Path | None:
    """Write ``status`` / ``approved_at`` into the existing profile.yaml without touching the
    user's comments or layout (backup ``profile.yaml.bak-<stamp>`` first); a full validated
    rewrite only when the targeted edit is not possible. Returns the backup path."""
    from kallimachos.config.yaml_edit import set_scalars

    plain = dump(profile)
    assert isinstance(plain, dict)
    backup = store.backup_copy(path)
    text = path.read_text(encoding="utf-8")
    edited = set_scalars(
        text, {("status",): plain["status"], ("approved_at",): plain["approved_at"]}
    )
    if edited is not None:
        try:
            check = build(Profile, yaml.safe_load(edited) or {}, path=path.name)
        except (SchemaViolation, yaml.YAMLError, ConfigError):
            check = None
        if check is not None and dump(check) == plain:
            store.write_text_atomic(path, edited)
            return backup
    write_yaml_validated(path, plain, "profile.schema.json")
    return backup


def cmd_approve(args: argparse.Namespace) -> int:
    """Set status: approved and approved_at."""
    _config, layout = load_config_and_layout(args)
    profile = load_profile(layout.profile_file)
    if not profile.channels:
        raise UsageError("the profile has no channels; build or edit it before approving")
    profile.status = "approved"
    profile.approved_at = _dt.datetime.now(_dt.UTC).replace(microsecond=0)
    if not args.dry_run:
        approve_in_place(layout.profile_file, profile)
    if args.json:
        print(
            json.dumps(
                {
                    "path": str(layout.profile_file),
                    "status": profile.status,
                    "approved_at": profile.approved_at.isoformat(),
                    "dry_run": bool(args.dry_run),
                }
            )
        )
    else:
        print(f"profile approved at {profile.approved_at.isoformat()} ({layout.profile_file})")
    return 0
