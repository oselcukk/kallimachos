"""``kallimachos init`` wizard.

The agent asks the questions in the user's language and passes ``answers.yaml``;
without ``--answers`` the command asks interactively. Never overwrites an existing config/profile
without ``--reconfigure`` plus approval (exit 5).

``answers.yaml`` is validated strictly through the ``Answers`` dataclasses below (dead keys are
errors). Secrets are accepted only as ``keyring:`` / ``env:`` references.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import jsonschema
import yaml

from kallimachos.config.schema import Config, RssFeed
from kallimachos.i18n import t
from kallimachos.interfaces import ConfigError, SchemaViolation, build, dump
from kallimachos.paths import DataLayout
from kallimachos.profile.qa import profile_from_answers
from kallimachos.profile.schema import Profile
from kallimachos.records.normalize import nfc

SOURCE_NAMES: tuple[str, ...] = (
    "arxiv",
    "iacr_eprint",
    "crossref",
    "openalex",
    "semantic_scholar",
    "zbmath",
    "pubmed",
    "biorxiv",
    "rss",
    "scholar_imap",
)
_FIELD_DEFAULT_SOURCES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (
        re.compile(r"(?i)\b(bio|medic|gene|genom|protein|cell|clinic|neuro|pharma|epidem)"),
        ("pubmed", "biorxiv", "crossref", "openalex"),
    ),
    (
        re.compile(r"(?i)\b(comput|algorithm|machine learning|software|network|robot|nlp|vision)"),
        ("arxiv", "crossref", "openalex", "semantic_scholar"),
    ),
)
_MATH_DEFAULT_SOURCES = ("arxiv", "crossref", "openalex", "zbmath")
_TIME_RE = re.compile(r"^[0-2][0-9]:[0-5][0-9]$")
_REF_RE = re.compile(r"^(keyring:[^\s]+|env:[A-Za-z_][A-Za-z0-9_]*)$")


# ---------------------------------------------------------------------------
# answers.yaml shape
# ---------------------------------------------------------------------------


@dataclass
class QaAnswers:
    field: str = ""
    channels: list[Any] = dataclasses.field(default_factory=list)
    authors: list[Any] = dataclasses.field(default_factory=list)
    journals: list[Any] = dataclasses.field(default_factory=list)
    arxiv_categories: list[str] = dataclasses.field(default_factory=list)
    codes: list[str] = dataclasses.field(default_factory=list)
    exclude: list[str] = dataclasses.field(default_factory=list)


@dataclass
class ProfileAnswers:
    methods: list[Literal["qa", "files", "orcid", "openalex"]] = field(
        default_factory=lambda: ["qa"]
    )
    qa: QaAnswers | None = None
    files: list[str] = field(default_factory=list)
    orcid: str | None = None
    openalex_author: str | None = None


@dataclass
class KeysAnswers:
    openalex: str | None = None
    semantic_scholar: str | None = None
    crossref_mailto: str | None = None
    unpaywall_email: str | None = None


@dataclass
class ModelAnswers:
    provider: Literal["claude", "codex", "hermes", "none"] | None = None  # null = detected
    command: str | None = None


@dataclass
class TelegramAnswers:
    bot_token_ref: str | None = None
    chat_id: str | int | None = None


@dataclass
class EmailAnswers:
    smtp_host: str | None = None
    smtp_port: int = 587
    security: Literal["starttls", "ssl"] = "starttls"
    username: str | None = None
    password_ref: str | None = None
    from_: str | None = field(default=None, metadata={"alias": "from"})
    to: list[str] = field(default_factory=list)


@dataclass
class RssFeedAnswer:
    name: str
    url: str
    channel_hint: str | None = None


@dataclass
class NotifyAnswers:
    channels: list[Literal["markdown", "telegram", "email"]] = field(
        default_factory=lambda: ["markdown"]
    )
    markdown_path: str | None = None
    telegram: TelegramAnswers | None = None
    email: EmailAnswers | None = None


@dataclass
class FeedbackAnswers:
    channels: list[Literal["telegram", "imap", "markdown"]] | None = None  # null = derived


@dataclass
class ArchiveAnswers:
    enabled: bool = False
    root: str | None = None
    scan_existing: list[str] = field(default_factory=list)


@dataclass
class ProcessAnswers:
    enabled: bool = False
    ocr: bool = False
    zotero_user_id: str | int | None = None
    zotero_api_key_ref: str | None = None


@dataclass
class NotesAnswers:
    target: Literal["avenox", "plain"] | None = None  # null = detected (beyin.py present?)
    vault_path: str | None = None
    literature_folder: str = "Literature"
    task_owner: str | None = None


@dataclass
class BridgeAnswers:
    enabled: bool = False
    remote: str | None = None
    remote_data_dir: str | None = None


@dataclass
class ScheduleAnswers:
    backend: Literal["local", "hermes", "github", "none"] | None = None
    daily_at: str = "07:30"
    weekly_on: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = "sun"
    weekly_at: str = "09:30"
    archive_at: str = "06:30"


@dataclass
class Answers:
    """Root of ``answers.yaml``. Unknown keys are rejected."""

    language: str = "en"
    data_dir: str | None = None
    timezone: str | None = None
    profile: ProfileAnswers = field(default_factory=ProfileAnswers)
    sources: list[str] | None = None  # null = default set by field guess
    rss_feeds: list[RssFeedAnswer] = field(default_factory=list)
    keys: KeysAnswers = field(default_factory=KeysAnswers)
    model: ModelAnswers = field(default_factory=ModelAnswers)
    notify: NotifyAnswers = field(default_factory=NotifyAnswers)
    feedback: FeedbackAnswers = field(default_factory=FeedbackAnswers)
    archive: ArchiveAnswers = field(default_factory=ArchiveAnswers)
    process: ProcessAnswers = field(default_factory=ProcessAnswers)
    notes: NotesAnswers = field(default_factory=NotesAnswers)
    bridge: BridgeAnswers = field(default_factory=BridgeAnswers)
    schedule: ScheduleAnswers = field(default_factory=ScheduleAnswers)


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Question:
    number: str  # "1", "3a", "11b"
    key: str  # answers.yaml path, e.g. "notify.channels"
    prompt_key: str  # i18n key
    choices: tuple[str, ...] = ()
    default: str | None = None
    multiple: bool = False


QUESTIONS: tuple[Question, ...] = (
    Question("1", "language", "wizard.q.language", ("en", "tr"), "en"),
    Question("2", "data_dir", "wizard.q.data_dir", (), None),
    Question(
        "3",
        "profile.methods",
        "wizard.q.profile_methods",
        ("qa", "files", "orcid", "openalex"),
        "qa",
        multiple=True,
    ),
    Question("3a", "profile.qa", "wizard.q.profile_qa"),
    Question("3b", "profile.files", "wizard.q.profile_files", multiple=True),
    Question("3c", "profile.orcid", "wizard.q.profile_id"),
    Question("4", "sources", "wizard.q.sources", SOURCE_NAMES, None, multiple=True),
    Question("5", "keys", "wizard.q.keys"),
    Question("6", "model.provider", "wizard.q.model", ("claude", "codex", "hermes", "none"), None),
    Question(
        "7",
        "notify.channels",
        "wizard.q.notify",
        ("markdown", "telegram", "email"),
        "markdown",
        multiple=True,
    ),
    Question(
        "8",
        "feedback.channels",
        "wizard.q.feedback",
        ("telegram", "imap", "markdown"),
        None,
        multiple=True,
    ),
    Question("9", "archive.enabled", "wizard.q.archive", ("yes", "no"), "no"),
    Question("10", "process.enabled", "wizard.q.process", ("yes", "no"), "no"),
    Question("11", "notes.target", "wizard.q.notes", ("avenox", "plain"), None),
    Question("11b", "bridge.enabled", "wizard.q.bridge", ("yes", "no"), "no"),
    Question(
        "12",
        "schedule.backend",
        "wizard.q.schedule",
        ("local", "hermes", "github", "none"),
        "local",
    ),
)
"""Wizard questions: 12 questions plus the 3a/3b/3c/11b sub-questions."""


def prompt_text(key: str, lang: str) -> str:
    """Question text from the ``wizard.q.*`` i18n keys (English fallback inside ``t``)."""
    try:
        return t(key, lang)
    except KeyError:
        return key


# ---------------------------------------------------------------------------
# Load / ask
# ---------------------------------------------------------------------------


def load_answers(path: Path) -> dict[str, Any]:
    """Read and strictly validate ``answers.yaml``."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: answers must be a mapping")
    answers = validate_answers(raw)
    data: dict[str, Any] = dump(answers)
    return data


def validate_answers(raw: Mapping[str, Any]) -> Answers:
    try:
        return build(Answers, raw)
    except SchemaViolation as exc:
        raise ConfigError(f"answers.yaml: {exc}") from None


def ask_interactively(
    lang: str,
    *,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Prompt on the terminal; returns an answers mapping (validated like ``answers.yaml``)."""
    say = output or (lambda text: print(text, file=sys.stderr))
    answers: dict[str, Any] = {}

    def ask(q: Question) -> str:
        choices = f" [{'/'.join(q.choices)}]" if q.choices else ""
        default = f" ({q.default})" if q.default is not None else ""
        try:
            reply = input_fn(f"{q.number}. {prompt_text(q.prompt_key, lang)}{choices}{default}: ")
        except EOFError:
            reply = ""
        return reply.strip()

    def put(key: str, value: Any) -> None:
        node = answers
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    for q in QUESTIONS:
        if q.number == "3a" and "qa" not in answers.get("profile", {}).get("methods", []):
            continue
        if q.number == "3b" and "files" not in answers.get("profile", {}).get("methods", []):
            continue
        if q.number == "3c" and not (
            {"orcid", "openalex"} & set(answers.get("profile", {}).get("methods", []))
        ):
            continue
        if q.number == "11" and not answers.get("process", {}).get("enabled"):
            continue
        reply = ask(q)
        if q.number == "1":
            lang = reply or (q.default or "en")
            put(q.key, lang)
        elif q.number == "3a":
            lines = [reply] if reply else []
            while True:
                more = ask(Question("3a", q.key, "wizard.q.profile_qa"))
                if not more:
                    break
                lines.append(more)
            put("profile.qa", {"field": lines[0].split(":")[0] if lines else "", "channels": lines})
        elif q.number == "3c":
            if reply.upper().startswith("A") and reply[1:].isdigit():
                put("profile.openalex_author", reply)
            elif reply:
                put("profile.orcid", reply)
        elif q.number == "5":
            keys: dict[str, str] = {}
            for part in re.split(r"[,\s]+", reply):
                name, sep, ref = part.partition("=")
                if sep and name in {f.name for f in dataclasses.fields(KeysAnswers)}:
                    keys[name] = ref
            put("keys", keys)
        elif q.choices and q.choices == ("yes", "no"):
            put(q.key, (reply or q.default or "no").lower().startswith("y"))
        elif q.multiple:
            values = [v for v in re.split(r"[,\s]+", reply) if v]
            if not values and q.default:
                values = [q.default]
            if values or q.number not in ("4", "8"):
                put(q.key, values)
        else:
            value = reply or q.default
            if value:
                put(q.key, value)
    say("")
    return dump(validate_answers(answers))  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Materialise
# ---------------------------------------------------------------------------


Provider = Literal["claude", "codex", "hermes", "none"]


def detect_agent(env: Mapping[str, str] | None = None) -> Provider:
    """Which agent runs the wizard: from well-known environment markers."""
    env = os.environ if env is None else env
    if any(k in env for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE")):
        return "claude"
    if any(k in env for k in ("CODEX_SANDBOX", "CODEX_CLI", "CODEX_HOME")):
        return "codex"
    if any(k in env for k in ("HERMES_HOME", "HERMES_AGENT")):
        return "hermes"
    return "none"


def detect_avenox(vault_path: str | None) -> bool:
    """Avenox vault = ``beyin.py`` at the vault root."""
    if not vault_path:
        return False
    return (Path(vault_path).expanduser() / "beyin.py").is_file()


def os_scheduler_name(platform: str | None = None) -> str:
    platform = platform or sys.platform
    if platform == "darwin":
        return "launchd"
    if platform.startswith("win"):
        return "Task Scheduler"
    return "systemd timer (cron fallback)"


def _ref(value: str | None, what: str) -> str | None:
    if value is None or value == "":
        return None
    if not _REF_RE.match(value):
        raise ConfigError(
            f"{what}: secrets must be references (keyring:<name> or env:<VAR>), got a literal value"
        )
    return value


def default_sources(field_text: str) -> tuple[str, ...]:
    for pattern, names in _FIELD_DEFAULT_SOURCES:
        if pattern.search(field_text):
            return names
    return _MATH_DEFAULT_SOURCES


def sensitive_folder_word(literature_folder: str) -> str | None:
    """The first path component of ``literature_folder`` that Avenox keeps out of its index
    (``process.avenox.sensitive_name``), or ``None``. Used by the wizard and ``doctor``."""
    from kallimachos.process.avenox import sensitive_name

    for part in Path(literature_folder).parts:
        if part not in ("", ".", "/") and sensitive_name(part):
            return part
    return None


def materialize(
    answers: dict[str, Any],
    layout: DataLayout,
    *,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> tuple[Config, Profile, list[str]]:
    """answers -> (config, draft profile, notes for the user). Detects the agent, Avenox and OS."""
    a = validate_answers(answers)
    notes: list[str] = []
    config = Config()
    config.language = a.language
    config.data_dir = a.data_dir
    config.timezone = a.timezone

    # q4 sources
    field_text = " ".join(
        [a.profile.qa.field, *(str(c) for c in a.profile.qa.channels)] if a.profile.qa else []
    )
    sources = tuple(a.sources) if a.sources is not None else default_sources(field_text)
    if a.sources is None:
        notes.append(f"sources defaulted from the field description: {', '.join(sources)}")
    for name in sources:
        if name not in SOURCE_NAMES:
            raise ConfigError(f"unknown source {name!r}; known: {', '.join(SOURCE_NAMES)}")
    for name in SOURCE_NAMES:
        getattr(config.sources, name).enabled = name in sources
    if a.rss_feeds:
        config.sources.rss.enabled = True
        config.sources.rss.feeds = [
            RssFeed(name=f.name, url=f.url, channel_hint=f.channel_hint) for f in a.rss_feeds
        ]

    # q5 keys (references only)
    config.sources.openalex.api_key_ref = _ref(a.keys.openalex, "keys.openalex")
    config.sources.semantic_scholar.api_key_ref = _ref(
        a.keys.semantic_scholar, "keys.semantic_scholar"
    )
    config.sources.crossref.mailto_ref = _ref(a.keys.crossref_mailto, "keys.crossref_mailto")
    config.sources.unpaywall.email_ref = _ref(a.keys.unpaywall_email, "keys.unpaywall_email")
    config.sources.unpaywall.enabled = config.sources.unpaywall.email_ref is not None

    # q6 model
    provider = a.model.provider or detect_agent(env)
    if a.model.provider is None:
        notes.append(f"model provider detected: {provider}")
    config.model.provider = provider
    config.model.command = a.model.command

    # q7 notify
    config.notify.channels = list(dict.fromkeys(a.notify.channels)) or ["markdown"]
    config.notify.markdown.path = a.notify.markdown_path
    if a.notify.telegram:
        config.notify.telegram.bot_token_ref = _ref(
            a.notify.telegram.bot_token_ref, "notify.telegram.bot_token_ref"
        )
        config.notify.telegram.chat_id = a.notify.telegram.chat_id
    if "telegram" in config.notify.channels and not config.notify.telegram.bot_token_ref:
        raise ConfigError("notify.telegram.bot_token_ref is required for the telegram channel")
    if a.notify.email:
        e = a.notify.email
        config.notify.email.smtp_host = e.smtp_host
        config.notify.email.smtp_port = e.smtp_port
        config.notify.email.security = e.security
        config.notify.email.username = e.username
        config.notify.email.password_ref = _ref(e.password_ref, "notify.email.password_ref")
        config.notify.email.from_ = e.from_
        config.notify.email.to = list(e.to)
    if "email" in config.notify.channels and not config.notify.email.smtp_host:
        raise ConfigError("notify.email.smtp_host is required for the email channel")

    # q8 feedback
    if a.feedback.channels is not None:
        config.feedback.channels = list(dict.fromkeys(a.feedback.channels))
    else:
        derived: list[Literal["telegram", "imap", "markdown"]] = []
        for ch in config.notify.channels:
            derived.append("imap" if ch == "email" else ch)
        config.feedback.channels = derived or ["markdown"]
        notes.append(f"feedback channels derived: {', '.join(config.feedback.channels)}")

    # q9 archive
    config.archive.enabled = a.archive.enabled
    config.archive.root = a.archive.root
    config.archive.existing_roots = list(a.archive.scan_existing)
    if a.archive.enabled and not a.archive.root:
        raise ConfigError("archive.root is required when the archive module is enabled")

    # q10 process
    config.process.enabled = a.process.enabled
    config.process.ocr_tool = "ocrmypdf" if a.process.ocr else "none"
    if a.process.zotero_user_id is not None:
        config.process.zotero.enabled = True
        config.process.zotero.user_id = a.process.zotero_user_id
        config.process.zotero.api_key_ref = _ref(
            a.process.zotero_api_key_ref, "process.zotero_api_key_ref"
        )

    # q11 notes
    avenox = detect_avenox(a.notes.vault_path)
    target = a.notes.target or ("avenox" if avenox else "plain")
    if a.notes.target is None and a.process.enabled:
        found = "found" if avenox else "absent"
        notes.append(f"notes target detected: {target} (beyin.py {found})")
    if target == "avenox" and not avenox:
        raise ConfigError("notes.target avenox requires beyin.py in notes.vault_path")
    config.notes.target = target
    config.notes.vault_path = a.notes.vault_path
    config.notes.literature_folder = a.notes.literature_folder
    sensitive = sensitive_folder_word(a.notes.literature_folder)
    if sensitive is not None:
        notes.append(
            t(
                "wizard.warn.sensitive_folder",
                a.language,
                folder=a.notes.literature_folder,
                word=sensitive,
            )
        )
    config.notes.task_owner = a.notes.task_owner

    # q11b bridge
    config.bridge.enabled = a.bridge.enabled
    config.bridge.remote = a.bridge.remote
    config.bridge.remote_data_dir = a.bridge.remote_data_dir
    if a.bridge.enabled and not (a.bridge.remote and a.bridge.remote_data_dir):
        raise ConfigError("bridge.remote and bridge.remote_data_dir are required for the bridge")

    # q12 schedule
    config.schedule.backend = a.schedule.backend or "local"
    for name in ("daily_at", "weekly_at", "archive_at"):
        value = getattr(a.schedule, name)
        if not _TIME_RE.match(value):
            raise ConfigError(f"schedule.{name} must be HH:MM, got {value!r}")
        setattr(config.schedule, name, value)
    config.schedule.weekly_on = a.schedule.weekly_on
    if config.schedule.backend == "local":
        notes.append(f"local scheduler on this OS: {os_scheduler_name(platform)}")

    # profile
    profile: Profile
    if "qa" in a.profile.methods and a.profile.qa is not None:
        profile, warnings = profile_from_answers(
            dump(a.profile.qa),
            language_hint=a.language,
            min_core_terms=config.profile.min_core_terms_warning,
        )
        notes.extend(warnings)
    else:
        profile = Profile(
            language_hint=a.language, field=a.profile.qa.field if a.profile.qa else ""
        )
        if "qa" in a.profile.methods:
            notes.append("profile.qa is empty; the profile draft has no channels yet")
    if "files" in a.profile.methods:
        paths = " ".join(a.profile.files) or "<paths>"
        notes.append(f"next: kallimachos profile build --from-files {paths}")
    if "orcid" in a.profile.methods:
        notes.append(f"next: kallimachos profile build --from-orcid {a.profile.orcid or '<orcid>'}")
    if "openalex" in a.profile.methods:
        notes.append(
            "next: kallimachos profile build --from-openalex "
            f"{a.profile.openalex_author or '<author id>'}"
        )
    return config, profile, notes


# ---------------------------------------------------------------------------
# Diff and YAML writing
# ---------------------------------------------------------------------------


def _flatten(data: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(data, Mapping):
        for key, value in data.items():
            out.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(data, list) and data and all(isinstance(v, Mapping) for v in data):
        for index, value in enumerate(data):
            out.update(_flatten(value, f"{prefix}[{index}]"))
    else:
        out[prefix] = data
    return out


def diff_mappings(old: Mapping[str, Any], new: Mapping[str, Any], label: str) -> list[str]:
    flat_old, flat_new = _flatten(old), _flatten(new)
    lines: list[str] = []
    for key in sorted(set(flat_old) | set(flat_new)):
        if key not in flat_old:
            lines.append(f"{label}: + {key} = {flat_new[key]!r}")
        elif key not in flat_new:
            lines.append(f"{label}: - {key} (was {flat_old[key]!r})")
        elif flat_old[key] != flat_new[key]:
            lines.append(f"{label}: {key}: {flat_old[key]!r} -> {flat_new[key]!r}")
    return lines


def read_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping")
    return raw


def diff_existing(layout: DataLayout, config: Config, profile: Profile) -> list[str]:
    """Human-readable differences against existing files for ``--reconfigure``."""
    lines: list[str] = []
    if layout.config_file.exists():
        lines.extend(diff_mappings(read_yaml_mapping(layout.config_file), dump(config), "config"))
    if layout.profile_file.exists():
        lines.extend(
            diff_mappings(read_yaml_mapping(layout.profile_file), dump(profile), "profile")
        )
    return lines


def schema_path(name: str) -> Path:
    """``schemas/<name>`` of the installed package (repository layout ``src/`` + ``schemas/``)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "schemas" / name
        if candidate.is_file():
            return candidate
    raise ConfigError(f"schema {name} not found next to the package")


def write_yaml_validated(path: Path, data: Mapping[str, Any], schema_name: str) -> None:
    """NFC + JSON-Schema validation + atomic write (tmp + ``os.replace``)."""
    plain = json.loads(nfc(json.dumps(data, ensure_ascii=False)))
    schema = json.loads(schema_path(schema_name).read_text(encoding="utf-8"))
    try:
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
            plain
        )
    except jsonschema.ValidationError as exc:
        raise ConfigError(f"{path.name}: {exc.message}") from None
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(plain, allow_unicode=True, sort_keys=False, default_flow_style=False)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
