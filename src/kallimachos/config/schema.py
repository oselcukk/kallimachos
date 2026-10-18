"""``config.yaml`` shape.

Every supported key is a dataclass field here; anything else is a dead key and
:func:`kallimachos.interfaces.build` rejects it (exit 2). Defaults are the documented defaults.
Semantic validation (interval floors, secret detection, ``notes.target: avenox`` requires
``beyin.py`` ...) lives in ``config/validate.py``; this module is shape only.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Literal

from kallimachos.interfaces import SCHEMA_REGISTRY, FieldClass, MessageBudget

# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------

DEFAULT_MIN_INTERVAL_S: dict[str, float] = {
    "arxiv": 3.1,
    "iacr_eprint": 1.0,
    "crossref": 0.6,
    "openalex": 0.15,
    "semantic_scholar": 1.2,
    "zbmath": 2.5,
    "pubmed": 0.35,
    "pubmed_keyed": 0.1,
    "biorxiv": 1.0,
    "rss": 1.0,
    "unpaywall": 1.0,
    "orcid": 0.1,
}
"""Per-source request interval floors. ``config validate`` rejects smaller values."""

SecretRef = str | None
"""``keyring:<service>`` or ``env:<VAR>``; never the secret itself."""


@dataclass
class ArxivConfig:
    enabled: bool = True
    window_days: int = 4
    page_size: int = 1000
    max_records: int = 12000
    min_interval_s: float = 3.1
    base_url: str = "https://export.arxiv.org/api/query"  # falls back to http on 3xx/error


@dataclass
class IacrEprintConfig:
    enabled: bool = False
    window_days: int = 4
    min_interval_s: float = 1.0


@dataclass
class CrossrefConfig:
    enabled: bool = True
    index_window_days: int = 1
    created_window_days: int = 30
    rows: int = 200
    min_interval_s: float = 0.6
    mailto_ref: SecretRef = None


@dataclass
class OpenAlexUnitCost:
    """USD per call by request kind."""

    list: float = 0.0001
    search: float = 0.001
    single: float = 0.0


@dataclass
class OpenAlexConfig:
    enabled: bool = True
    window_days: int = 1
    per_page: int = 200
    max_pages: int = 2
    min_interval_s: float = 0.15
    daily_budget_usd: float = 0.20
    weekly_extra_usd: float = 0.30
    daily_cap_usd: float = 0.90
    unit_cost_usd: OpenAlexUnitCost = field(default_factory=OpenAlexUnitCost)
    api_key_ref: SecretRef = None
    premium_filters: bool = False  # from_created_date / from_updated_date need a paid plan


@dataclass
class SemanticScholarConfig:
    enabled: bool = False
    min_interval_s: float = 1.2
    api_key_ref: SecretRef = None


@dataclass
class ZbmathConfig:
    enabled: bool = False
    window_days: int = 8
    max_pages_per_set: int = 15
    oldest_year_delta: int = 2
    timeout_s: int = 240
    min_interval_s: float = 2.5
    oai_set_template: str = "{msc2}"  # unverified OAI set syntax; adjustable


@dataclass
class PubmedConfig:
    enabled: bool = False
    window_days: int = 2
    min_interval_s: float = 0.35
    api_key_ref: SecretRef = None
    tool: str = "kallimachos"  # NCBI asks for tool + email registration
    email_ref: SecretRef = None


@dataclass
class BiorxivConfig:
    enabled: bool = False
    servers: list[Literal["biorxiv", "medrxiv"]] = field(default_factory=lambda: ["biorxiv"])
    window_days: int = 2
    min_interval_s: float = 1.0


@dataclass
class RssFeed:
    name: str
    url: str
    channel_hint: str | None = None


@dataclass
class RssConfig:
    enabled: bool = False
    window_days: int = 7
    min_interval_s: float = 1.0
    feeds: list[RssFeed] = field(default_factory=list)


@dataclass
class ScholarImapConfig:
    enabled: bool = False
    host: str | None = None
    port: int = 993
    user_ref: SecretRef = None
    password_ref: SecretRef = None
    folder: str = "INBOX"
    sender_filter: str = "scholaralerts-noreply@google.com"  # unverified; wizard confirms


@dataclass
class UnpaywallConfig:
    """Archive only; enabled when an email reference is given."""

    enabled: bool = False
    email_ref: SecretRef = None
    min_interval_s: float = 1.0


@dataclass
class OrcidConfig:
    """Profile building only."""

    token_ref: SecretRef = None
    min_interval_s: float = 0.1


@dataclass
class SourcesConfig:
    arxiv: ArxivConfig = field(default_factory=ArxivConfig)
    iacr_eprint: IacrEprintConfig = field(default_factory=IacrEprintConfig)
    crossref: CrossrefConfig = field(default_factory=CrossrefConfig)
    openalex: OpenAlexConfig = field(default_factory=OpenAlexConfig)
    semantic_scholar: SemanticScholarConfig = field(default_factory=SemanticScholarConfig)
    zbmath: ZbmathConfig = field(default_factory=ZbmathConfig)
    pubmed: PubmedConfig = field(default_factory=PubmedConfig)
    biorxiv: BiorxivConfig = field(default_factory=BiorxivConfig)
    rss: RssConfig = field(default_factory=RssConfig)
    scholar_imap: ScholarImapConfig = field(default_factory=ScholarImapConfig)
    unpaywall: UnpaywallConfig = field(default_factory=UnpaywallConfig)
    orcid: OrcidConfig = field(default_factory=OrcidConfig)


# ---------------------------------------------------------------------------
# scoring / ranking / rescan
# ---------------------------------------------------------------------------


@dataclass
class ScoringWeights:
    """Numeric weights of the topic score."""

    title_core: float = 6
    abstract_core: float = 3
    title_broad: float = 2
    abstract_broad: float = 1
    channel_cap: float = 18
    penalty: float = -3
    penalty_floor: float = -6
    arxiv_primary_category: float = 1.0
    arxiv_secondary_category: float = 0.5
    msc_primary_main: float = 3
    msc_primary: float = 1.5
    msc_secondary: float = 0.5
    follow_author: float = 5
    always_show_author: float = 100
    seed_citation: float = 4
    seed_citation_extra: float = 1
    seed_citation_cap: float = 7
    foundational_citation: float = 3
    foundational_citation_extra: float = 1
    foundational_citation_cap: float = 5
    full_journal: float = 3
    arxiv_category_mismatch_factor: float = 0.6


@dataclass
class VenueIndicatorConfig:
    """Thresholds for the open-data venue indicator."""

    strong_min_citedness: float = 1.0
    weak_max_citedness: float = 0.3
    weak_min_works: int = 500
    cache_ttl_days: int = 90


@dataclass
class ScoringConfig:
    weights: ScoringWeights = field(default_factory=ScoringWeights)
    priority_multiplier: dict[int, float] = field(default_factory=lambda: {1: 1.0, 2: 0.85, 3: 0.7})
    threshold: dict[int, float] = field(default_factory=lambda: {1: 4, 2: 6, 3: 9})
    noise_factor: float = 0.35
    noise_abstract_min_terms: int = 2
    certain_pass_factor: float = 1.5
    certain_pass_factor_by_channel: dict[str, float] = field(default_factory=dict)
    certain_pass_fields: list[FieldClass] = field(
        default_factory=lambda: [FieldClass.MATH, FieldClass.MATH_PHYS]
    )
    paratext_extra: list[str] = field(default_factory=list)
    repository_venues_extra: list[str] = field(default_factory=list)
    venue_indicator: VenueIndicatorConfig = field(default_factory=VenueIndicatorConfig)


@dataclass
class RankingConfig:
    candidates: int = 30
    main_items: int = 10
    near_miss: int = 3
    journal_quota: int = 2
    journal_quota_pool: int = 6
    abstract_full_first: int = 15
    abstract_max_chars: int = 1500
    abstract_short_chars: int = 300


@dataclass
class RescanConfig:
    unshown_grace_days: int = 7


# ---------------------------------------------------------------------------
# model / notify / feedback
# ---------------------------------------------------------------------------


@dataclass
class ModelConfig:
    provider: Literal["claude", "codex", "hermes", "none"] = "none"
    command: str | None = None  # null = provider default
    timeout_s: int = 600
    prompt_dir: str | None = None  # null = packaged prompts


@dataclass
class MarkdownNotifyConfig:
    path: str | None = None  # null = <data_dir>/daily
    checkbox_feedback: bool = True


@dataclass
class TelegramNotifyConfig:
    bot_token_ref: SecretRef = None
    chat_id: str | int | None = None


@dataclass
class EmailNotifyConfig:
    smtp_host: str | None = None
    smtp_port: int = 587
    security: Literal["starttls", "ssl"] = "starttls"
    username: str | None = None
    password_ref: SecretRef = None
    from_: str | None = field(default=None, metadata={"alias": "from"})
    to: list[str] = field(default_factory=list)
    confirmed_at: _dt.datetime | None = None  # set by `notify confirm-email`
    imap_host: str | None = None  # feedback by e-mail reply; null = derived from smtp_host
    imap_port: int = 993
    imap_folder: str = "INBOX"


@dataclass
class NotifyConfig:
    silent_when_empty: bool = False
    channels: list[Literal["markdown", "telegram", "email"]] = field(
        default_factory=lambda: ["markdown"]
    )
    markdown: MarkdownNotifyConfig = field(default_factory=MarkdownNotifyConfig)
    telegram: TelegramNotifyConfig = field(default_factory=TelegramNotifyConfig)
    email: EmailNotifyConfig = field(default_factory=EmailNotifyConfig)
    budget: MessageBudget = field(default_factory=MessageBudget)


@dataclass
class WeightLearningConfig:
    """Weight learning from feedback: ``propose`` reports changes, ``auto`` applies them."""

    mode: Literal["propose", "auto"] = "propose"
    learning_rate: float = 0.15
    min_evidence: int = 3
    clamp: tuple[float, float] = field(default=(0.5, 1.6), metadata={"minItems": 2, "maxItems": 2})


@dataclass
class FeedbackConfig:
    channels: list[Literal["telegram", "imap", "markdown"]] = field(
        default_factory=lambda: ["markdown"]
    )
    weights: WeightLearningConfig = field(default_factory=WeightLearningConfig)


# ---------------------------------------------------------------------------
# archive / process / notes / bridge / schedule / privacy / profile
# ---------------------------------------------------------------------------

DEFAULT_SENSITIVE_PATH_REGEX = (
    r"(?i)\b(personal|private|özel|kimlik|passport|salary|maaş|health|sağlık|"
    r"students?|öğrenci|exams?|sınav|homework|ödev)\b"
)
"""Word-bounded so that ``Example`` or ``examination-free`` paths are not hidden (lead decision)."""


@dataclass
class ArchiveConfig:
    enabled: bool = False
    root: str | None = None
    naming_template: str = "{authors}_{year}_{short_title}[_arXiv-{arxiv}{version}]"
    max_authors_in_name: int = 3
    short_title_words: int = 7
    short_title_chars: int = 60
    max_pdf_mb: int = 150
    existing_roots: list[str] = field(default_factory=list)
    existing_exclude: list[str] = field(default_factory=list)
    sensitive_path_regex: list[str] = field(default_factory=lambda: [DEFAULT_SENSITIVE_PATH_REGEX])


@dataclass
class ChunkConfig:
    size: int = 100
    group_size: int = 3


@dataclass
class ProcessModelsConfig:
    """Role -> model name. Documentation only; the agent lead applies roles."""

    reader: str | None = None
    recorder: str | None = None
    verifier: str | None = None
    cluster: str | None = None


@dataclass
class ZoteroConfig:
    enabled: bool = False
    user_id: str | int | None = None
    api_key_ref: SecretRef = None
    library_type: Literal["user", "group"] = "user"
    base_dir: str | None = None


@dataclass
class ProcessConfig:
    enabled: bool = False
    text_tool: Literal["pdftotext"] = "pdftotext"
    ocr_tool: Literal["ocrmypdf", "none"] = "none"
    ocr_langs: list[str] = field(default_factory=lambda: ["eng"])
    workers: int = 4
    quote_threshold: int = 90
    book_pages: int = 150
    chunk: ChunkConfig = field(default_factory=ChunkConfig)
    models: ProcessModelsConfig = field(default_factory=ProcessModelsConfig)
    verify_sample_ratio: float = 0.10
    zotero: ZoteroConfig = field(default_factory=ZoteroConfig)


@dataclass
class NotesFrontmatterConfig:
    kind: str = "note"
    project: str | None = None  # null = profile.field
    visibility: Literal["public", "internal", "private"] = "internal"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class NotesConfig:
    target: Literal["avenox", "plain"] = "plain"
    vault_path: str | None = None
    literature_folder: str = "Literature"
    link_style: Literal["wikilink", "markdown"] | None = None  # null = by target
    language: str | None = None  # null = top-level language
    frontmatter: NotesFrontmatterConfig = field(default_factory=NotesFrontmatterConfig)
    etal_suffix: str = "etal"
    personal_relation_section: bool = True
    personal_profile_text: str | None = None
    cluster_summary_name: str = "_Cluster-Summary.md"
    reading_queue_file: str = "_Reading-Queue.md"
    task_owner: str | None = None


@dataclass
class BridgeConfig:
    """Optional server -> computer delivery bridge."""

    enabled: bool = False
    mode: Literal["pull"] = "pull"
    remote: str | None = None  # user@host, SSH key auth only
    remote_data_dir: str | None = None
    local_archive_root: str | None = None  # null = archive.root
    ssh_options: list[str] = field(default_factory=lambda: ["BatchMode=yes", "ConnectTimeout=20"])
    retry: int = 1


Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass
class ScheduleConfig:
    backend: Literal["local", "hermes", "github", "none"] = "local"
    daily_at: str = field(default="07:30", metadata={"pattern": "^[0-2][0-9]:[0-5][0-9]$"})
    weekly_on: Weekday = "sun"
    weekly_at: str = field(default="09:30", metadata={"pattern": "^[0-2][0-9]:[0-5][0-9]$"})
    archive_at: str = field(default="06:30", metadata={"pattern": "^[0-2][0-9]:[0-5][0-9]$"})
    catch_up_missed: bool = True
    max_catch_up_days: int = 3


@dataclass
class HooksConfig:
    """Optional agent hooks (``kallimachos hooks install``): a short SessionStart status
    summary and a PreToolUse guard for state files and managed note blocks. Where they are
    installed is recorded by ``hooks install`` in ``state/hooks.json``, not here."""

    session_start: bool = True
    guard: bool = True
    summary_max_chars: int = field(default=600, metadata={"minimum": 120, "maximum": 2000})


@dataclass
class PrivacyConfig:
    """Fixed values: the shape itself rejects any other value."""

    send_only_strong_ids: Literal[True] = True
    personal_data_in_logs: Literal[False] = False


@dataclass
class ProfileMatchConfig:
    """Match confidence thresholds for profile building."""

    autocomplete_min_sim: float = 0.7
    high_sim: float = 0.92
    medium_sim: float = 0.85
    medium_sim_strict: float = 0.97
    low_sim: float = 0.75
    medium_year_tolerance: int = 1
    strict_year_tolerance: int = 3
    old_year_gap: int = 10
    old_year_gap_sim: float = 0.95


@dataclass
class ProfileBuildConfig:
    match: ProfileMatchConfig = field(default_factory=ProfileMatchConfig)
    journal_full_max_yearly_volume: int = 300  # above this a journal becomes topic_filtered
    min_core_terms_warning: int = 5
    foundational_max: int = 200


# ---------------------------------------------------------------------------
# root
# ---------------------------------------------------------------------------


@dataclass
class Config:
    """Root of ``config.yaml``. Secrets never live here, only ``*_ref`` references."""

    schema_version: int = 1
    language: str = "en"
    timezone: str | None = None  # null = system
    data_dir: str | None = None  # null = OS default (paths.py)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    ranking: RankingConfig = field(default_factory=RankingConfig)
    rescan: RescanConfig = field(default_factory=RescanConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    feedback: FeedbackConfig = field(default_factory=FeedbackConfig)
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    process: ProcessConfig = field(default_factory=ProcessConfig)
    notes: NotesConfig = field(default_factory=NotesConfig)
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    hooks: HooksConfig = field(default_factory=HooksConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    profile: ProfileBuildConfig = field(default_factory=ProfileBuildConfig)


CONFIG_SCHEMA_VERSION = 1

SCHEMA_REGISTRY["config.schema.json"] = (
    Config,
    "https://github.com/oselcukk/kallimachos/schemas/config/1",
)
