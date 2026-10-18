"""Shared types, protocols and serialisation contract for every Kallimachos module.

Other modules import from it. A new shared field is added here, and then
``schemas/`` (``python tools/gen_schemas.py``); ``tests/test_schemas.py`` fails until
the two agree.

Contents, in order:

1. Exit codes and the exception hierarchy that maps onto them.
2. ``build`` / ``dump`` / ``json_schema``: one strict, reflective serialisation layer.
   Unknown keys are errors everywhere (the "dead key" rule, applied to every record).
3. Enumerations shared by several modules (work kinds, work states, feedback tags ...).
4. Discovery types: ``Window``, ``Ident``, ``Record``, the ``Source`` protocol.
5. Rule chain types, ``Candidate`` and ``RunResult``.
6. Feedback, seen and weights records.
7. Model ``Provider`` and ``Notifier`` protocols.
8. Archive catalog records.
9. Processing records: card/4, semantic_result/1, note/1, cluster/1, queue, graph.

Dates are ``datetime.date`` / ``datetime.datetime`` on the Python side and ISO-8601 strings
in JSON. Sets serialise as sorted lists. Enum members serialise as their string value.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import sys
import types
import typing
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar, runtime_checkable

if TYPE_CHECKING:
    from kallimachos.profile.schema import Profile

__all__ = [
    "CATALOG_PATH_PATTERN",
    "EID_PATTERN",
    "SCHEMA_REGISTRY",
    "ApprovalRequired",
    "ArchiveLogLine",
    "ArchiveTrigger",
    "Author",
    "BudgetExhausted",
    "Candidate",
    "Card",
    "CardCitation",
    "CardFlags",
    "CardItem",
    "CardReference",
    "CatalogEntry",
    "CatalogEvent",
    "CheckResult",
    "ClusterFrontmatter",
    "ConfigError",
    "FalseAlarm",
    "FeedbackChannel",
    "FeedbackEntry",
    "FeedbackIntake",
    "FeedbackTag",
    "FieldClass",
    "GraphEdge",
    "HttpClient",
    "HttpError",
    "HttpResponse",
    "Ident",
    "IdentKind",
    "ItemType",
    "KallimachosError",
    "LockHeld",
    "Message",
    "MessageBudget",
    "MessageItem",
    "Mismatch",
    "NearNote",
    "NoteFrontmatter",
    "Notifier",
    "NotifyResult",
    "OaCandidate",
    "PartialFailure",
    "ProcessedEntry",
    "Provider",
    "QueueSync",
    "RawFeedback",
    "ReadingQueueEntry",
    "Record",
    "Rule",
    "RuleContext",
    "RuleStage",
    "RuleTraceEntry",
    "RunKind",
    "RunResult",
    "SchemaViolation",
    "SeenEntry",
    "Selection",
    "SemanticResult",
    "Source",
    "SourceStats",
    "SourceTest",
    "StatusEntry",
    "TextMeta",
    "UnreachableEntry",
    "UsageError",
    "Verdict",
    "VerdictKind",
    "Weights",
    "Window",
    "WorkKind",
    "WorkState",
    "build",
    "dump",
    "json_schema",
]


# ---------------------------------------------------------------------------
# 1. Exit codes and exceptions
# ---------------------------------------------------------------------------


class ExitCode(enum.IntEnum):
    """Process exit codes shared by every CLI command."""

    OK = 0
    UNEXPECTED = 1
    USAGE = 2  # invalid config/profile or usage error
    PARTIAL = 3  # one source/channel failed, result still written
    LOCKED = 4  # another run holds the lock
    APPROVAL_REQUIRED = 5  # profile not approved, email not confirmed, user-file touch not approved
    EXHAUSTED = 6  # network/budget exhausted, no result at all


class KallimachosError(Exception):
    """Base class for errors that carry an exit code. ``cli.main`` maps them to the process exit."""

    exit_code: ExitCode = ExitCode.UNEXPECTED


class UsageError(KallimachosError):
    """Invalid configuration, profile or command usage (exit 2)."""

    exit_code = ExitCode.USAGE


class ConfigError(UsageError):
    """config.yaml / profile.yaml is invalid: dead key, bad type, secret in config, limit below
    floor."""


class SchemaViolation(UsageError):
    """Data does not match the expected shape. ``path`` is
    the dotted location of the offending key."""

    def __init__(self, message: str, path: str = "") -> None:
        self.path = path
        super().__init__(f"{path}: {message}" if path else message)


class PartialFailure(KallimachosError):
    """A source or channel failed but the result was still written (exit 3)."""

    exit_code = ExitCode.PARTIAL


class LockHeld(KallimachosError):
    """Another Kallimachos process holds the lock (exit 4)."""

    exit_code = ExitCode.LOCKED


class ApprovalRequired(KallimachosError):
    """User approval is missing (exit 5): unapproved profile, unconfirmed email, user file touch."""

    exit_code = ExitCode.APPROVAL_REQUIRED


class BudgetExhausted(KallimachosError):
    """Network or budget exhausted with no result (exit 6)."""

    exit_code = ExitCode.EXHAUSTED


# ---------------------------------------------------------------------------
# 2. Strict serialisation: build / dump / json_schema
# ---------------------------------------------------------------------------

T = TypeVar("T")

_NoneType = type(None)


def _is_dataclass_type(tp: Any) -> bool:
    return isinstance(tp, type) and dataclasses.is_dataclass(tp)


def _type_hints(cls: type) -> dict[str, Any]:
    # Resolve postponed annotations against the defining module so nested dataclasses and
    # TYPE_CHECKING-only imports do not break the builder.
    ns = dict(vars(sys.modules[cls.__module__]))
    ns.setdefault("Profile", Any)
    return typing.get_type_hints(cls, globalns=ns, include_extras=False)


def _unwrap_optional(tp: Any) -> tuple[Any, bool]:
    """Return (inner_type, is_optional) for ``X | None`` / ``Optional[X]``; else (tp, False)."""
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        args = typing.get_args(tp)
        non_none = [a for a in args if a is not _NoneType]
        if len(non_none) < len(args):
            inner = non_none[0] if len(non_none) == 1 else typing.Union[tuple(non_none)]  # noqa: UP007
            return inner, True
    return tp, False


def build(cls: type[T], data: Any, *, path: str = "") -> T:
    """Construct dataclass ``cls`` from a mapping, strictly.

    * unknown keys raise :class:`SchemaViolation` (dead-key rule),
    * missing keys without a default raise :class:`SchemaViolation`,
    * scalar types are checked (``bool`` is not accepted for ``int``; ``int`` is accepted for
    ``float``),
    * dates accept ISO strings or ``date``/``datetime`` instances,
    * enums accept their string value, ``Literal`` accepts one of its values,
    * ``list[X]``, ``set[X]``, ``dict[K, V]`` and nested dataclasses recurse.
    """
    return typing.cast(T, _build_value(cls, data, path))


def _build_value(tp: Any, value: Any, path: str) -> Any:
    inner, optional = _unwrap_optional(tp)
    if value is None:
        if optional or inner is Any or inner is _NoneType:
            return None
        raise SchemaViolation("null is not allowed", path)
    tp = inner
    if tp is Any:
        return value
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        errors: list[str] = []
        for alt in typing.get_args(tp):
            try:
                return _build_value(alt, value, path)
            except SchemaViolation as exc:
                errors.append(str(exc))
        raise SchemaViolation(f"no union alternative matched: {errors}", path)
    if origin is Literal:
        choices = typing.get_args(tp)
        if value not in choices:
            raise SchemaViolation(f"expected one of {list(choices)}, got {value!r}", path)
        return value
    if _is_dataclass_type(tp):
        return _build_dataclass(tp, value, path)
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        try:
            return tp(value)
        except ValueError:
            raise SchemaViolation(
                f"expected one of {[m.value for m in tp]}, got {value!r}", path
            ) from None
    if origin in (list, Sequence, typing.Sequence) or tp is list:
        if not isinstance(value, list):
            raise SchemaViolation(f"expected list, got {type(value).__name__}", path)
        (item_tp,) = typing.get_args(tp) or (Any,)
        return [_build_value(item_tp, v, f"{path}[{i}]") for i, v in enumerate(value)]
    if origin in (set, frozenset) or tp in (set, frozenset):
        if not isinstance(value, list | set | frozenset | tuple):
            raise SchemaViolation(f"expected list, got {type(value).__name__}", path)
        (item_tp,) = typing.get_args(tp) or (Any,)
        items = [_build_value(item_tp, v, f"{path}[{i}]") for i, v in enumerate(value)]
        return frozenset(items) if (origin or tp) is frozenset else set(items)
    if origin in (tuple,):
        if not isinstance(value, list | tuple):
            raise SchemaViolation(f"expected list, got {type(value).__name__}", path)
        args = typing.get_args(tp)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_build_value(args[0], v, f"{path}[{i}]") for i, v in enumerate(value))
        if len(args) != len(value):
            raise SchemaViolation(f"expected {len(args)} items, got {len(value)}", path)
        return tuple(
            _build_value(a, v, f"{path}[{i}]")
            for i, (a, v) in enumerate(zip(args, value, strict=True))
        )
    if origin in (dict, Mapping, typing.Mapping) or tp is dict:
        if not isinstance(value, Mapping):
            raise SchemaViolation(f"expected mapping, got {type(value).__name__}", path)
        key_tp, val_tp = typing.get_args(tp) or (Any, Any)
        out: dict[Any, Any] = {}
        for k, v in value.items():
            key = _build_key(key_tp, k, path)
            out[key] = _build_value(val_tp, v, f"{path}.{k}" if path else str(k))
        return out
    if tp is bool:
        if not isinstance(value, bool):
            raise SchemaViolation(f"expected bool, got {type(value).__name__}", path)
        return value
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise SchemaViolation(f"expected int, got {type(value).__name__}", path)
        return value
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise SchemaViolation(f"expected number, got {type(value).__name__}", path)
        return float(value)
    if tp is str:
        if not isinstance(value, str):
            raise SchemaViolation(f"expected string, got {type(value).__name__}", path)
        return value
    if tp is _dt.datetime:
        if isinstance(value, _dt.datetime):
            return value
        if isinstance(value, str):
            try:
                return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise SchemaViolation(f"expected ISO datetime, got {value!r}", path) from None
        raise SchemaViolation(f"expected ISO datetime, got {type(value).__name__}", path)
    if tp is _dt.date:
        if isinstance(value, _dt.datetime):
            return value.date()
        if isinstance(value, _dt.date):
            return value
        if isinstance(value, str):
            try:
                return _dt.date.fromisoformat(value)
            except ValueError:
                raise SchemaViolation(f"expected ISO date, got {value!r}", path) from None
        raise SchemaViolation(f"expected ISO date, got {type(value).__name__}", path)
    raise SchemaViolation(f"unsupported type {tp!r} in schema", path)


def _build_key(key_tp: Any, key: Any, path: str) -> Any:
    if key_tp is int:
        if isinstance(key, bool):
            raise SchemaViolation(f"expected integer key, got {key!r}", path)
        if isinstance(key, int):
            return key
        if isinstance(key, str) and key.lstrip("-").isdigit():
            return int(key)
        raise SchemaViolation(f"expected integer key, got {key!r}", path)
    if key_tp is str or key_tp is Any:
        if isinstance(key, str):
            return key
        if isinstance(key, int | float) and not isinstance(key, bool):
            return str(key)
        raise SchemaViolation(f"expected string key, got {key!r}", path)
    if isinstance(key_tp, type) and issubclass(key_tp, enum.Enum):
        return _build_value(key_tp, key, path)
    raise SchemaViolation(f"unsupported key type {key_tp!r}", path)


def _build_dataclass(cls: type[Any], value: Any, path: str) -> Any:
    if not isinstance(value, Mapping):
        raise SchemaViolation(
            f"expected mapping for {cls.__name__}, got {type(value).__name__}", path
        )
    hints = _type_hints(cls)
    fields = {_key_of(f): f for f in dataclasses.fields(cls) if f.init}
    unknown = sorted(set(value) - set(fields))
    if unknown:
        raise SchemaViolation(f"unknown key(s) {unknown} for {cls.__name__}", path)
    kwargs: dict[str, Any] = {}
    for key, f in fields.items():
        sub = f"{path}.{key}" if path else key
        if key in value:
            kwargs[f.name] = _build_value(hints[f.name], value[key], sub)
        elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            raise SchemaViolation("missing required key", sub)
    return cls(**kwargs)


def _key_of(f: dataclasses.Field[Any]) -> str:
    """Serialized key of a field: ``metadata["alias"]`` when set (e.g. ``from``), else the name."""
    alias = f.metadata.get("alias")
    return str(alias) if alias else f.name


def dump(obj: Any) -> Any:
    """Convert dataclasses/enums/dates/sets into plain JSON-compatible data.

    Mapping keys become strings (JSON objects only have string keys). Sets become sorted lists.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {_key_of(f): dump(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, _dt.datetime | _dt.date):
        return obj.isoformat()
    if isinstance(obj, Mapping):
        return {str(dump(k)): dump(v) for k, v in obj.items()}
    if isinstance(obj, set | frozenset):
        return sorted(dump(v) for v in obj)
    if isinstance(obj, list | tuple):
        return [dump(v) for v in obj]
    if obj is None or isinstance(obj, str | int | float | bool):
        return obj
    raise TypeError(f"cannot dump {type(obj).__name__}")


_SCHEMA_META_KEYS = (  # "alias" is handled separately: it renames the key
    "pattern",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "format",
    "description",
    "propertyNames",
    "const",
)


def json_schema(cls: type[Any], *, schema_id: str | None = None) -> dict[str, Any]:
    """Produce a JSON Schema (draft 2020-12) for dataclass ``cls``.

    Field ``metadata`` keys listed in ``_SCHEMA_META_KEYS`` are copied verbatim into the
    property schema (``pattern``, ``minimum`` ...). Dataclass docstrings become descriptions.
    Nested dataclasses go to ``$defs``.
    """
    defs: dict[str, Any] = {}
    root = _schema_for_dataclass(cls, defs)
    out: dict[str, Any] = {"$schema": "https://json-schema.org/draft/2020-12/schema"}
    if schema_id:
        out["$id"] = schema_id
    out["title"] = cls.__name__
    out.update(root)
    if defs:
        out["$defs"] = dict(sorted(defs.items()))
    return out


def _schema_for_dataclass(cls: type[Any], defs: dict[str, Any]) -> dict[str, Any]:
    hints = _type_hints(cls)
    props: dict[str, Any] = {}
    required: list[str] = []
    for f in dataclasses.fields(cls):
        if not f.init:
            continue
        prop = _schema_for_type(hints[f.name], defs)
        for key in _SCHEMA_META_KEYS:
            if key in f.metadata:
                prop[key] = f.metadata[key]
        if f.default is not dataclasses.MISSING:
            prop["default"] = dump(f.default)
        elif f.default_factory is dataclasses.MISSING:
            required.append(_key_of(f))
        props[_key_of(f)] = prop
    schema: dict[str, Any] = {"type": "object", "additionalProperties": False, "properties": props}
    if required:
        schema["required"] = required
    doc = (cls.__doc__ or "").strip().splitlines()
    if doc and doc[0]:
        schema["description"] = doc[0]
    return schema


def _schema_for_type(tp: Any, defs: dict[str, Any]) -> dict[str, Any]:
    inner, optional = _unwrap_optional(tp)
    if optional:
        return {"anyOf": [_schema_for_type(inner, defs), {"type": "null"}]}
    origin = typing.get_origin(tp)
    if tp is Any:
        return {}
    if tp is _NoneType:
        return {"type": "null"}
    if origin in (typing.Union, types.UnionType):
        return {"anyOf": [_schema_for_type(a, defs) for a in typing.get_args(tp)]}
    if origin is Literal:
        return {"enum": list(typing.get_args(tp))}
    if _is_dataclass_type(tp):
        name = tp.__name__
        if name not in defs:
            defs[name] = {}  # placeholder guards against recursion
            defs[name] = _schema_for_dataclass(tp, defs)
        return {"$ref": f"#/$defs/{name}"}
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        return {"type": "string", "enum": [m.value for m in tp]}
    if origin in (list, Sequence, typing.Sequence, tuple) or tp is list:
        args = typing.get_args(tp)
        item = args[0] if args else Any
        return {"type": "array", "items": _schema_for_type(item, defs)}
    if origin in (set, frozenset) or tp in (set, frozenset):
        args = typing.get_args(tp)
        item = args[0] if args else Any
        return {"type": "array", "uniqueItems": True, "items": _schema_for_type(item, defs)}
    if origin in (dict, Mapping, typing.Mapping) or tp is dict:
        args = typing.get_args(tp)
        key_tp, val_tp = args if args else (Any, Any)
        schema: dict[str, Any] = {
            "type": "object",
            "additionalProperties": _schema_for_type(val_tp, defs),
        }
        if key_tp is int:
            schema["propertyNames"] = {"pattern": "^-?[0-9]+$"}
        elif isinstance(key_tp, type) and issubclass(key_tp, enum.Enum):
            schema["propertyNames"] = {"enum": [m.value for m in key_tp]}
        return schema
    if tp is bool:
        return {"type": "boolean"}
    if tp is int:
        return {"type": "integer"}
    if tp is float:
        return {"type": "number"}
    if tp is str:
        return {"type": "string"}
    if tp is _dt.datetime:
        return {"type": "string", "format": "date-time"}
    if tp is _dt.date:
        return {"type": "string", "format": "date"}
    raise TypeError(f"unsupported type for JSON schema: {tp!r}")


# ---------------------------------------------------------------------------
# 3. Shared enumerations
# ---------------------------------------------------------------------------

EID_PATTERN = r"^[0-9a-f]{12}$"
CATALOG_PATH_PATTERN = r"^(?![/\\])(?!\.\.(?:[/\\]|$))(?!.*[/\\]\.\.(?:[/\\]|$)).+$"
"""A catalog ``path``: relative to the archive root, no leading slash and no ``..`` component."""
"""eid = sha256(pdf)[:12]."""

RUN_ID_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}-(daily|weekly)$"
"""run_id = YYYY-MM-DD-daily | YYYY-MM-DD-weekly."""


class RunKind(enum.StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"


class WorkKind(enum.StrEnum):
    """Kind of a bibliographic work. Catalog uses
    article|preprint|book|thesis|chapter."""

    ARTICLE = "article"
    PREPRINT = "preprint"
    BOOK = "book"
    THESIS = "thesis"
    CHAPTER = "chapter"
    UNKNOWN = "unknown"


class WorkState(enum.StrEnum):
    """States of the candidate -> work state machine. Transitions: state/machine.py."""

    DISCOVERED = "discovered"
    SCORED = "scored"
    DROPPED = "dropped"
    CANDIDATE = "candidate"
    SHOWN = "shown"
    UNSHOWN = "unshown"
    EXPIRED = "expired"
    FB_GOOD = "fb:good"
    FB_IRRELEVANT = "fb:irrelevant"
    FB_READ = "fb:read"
    FB_ASK = "fb:ask"
    FB_ARCHIVE = "fb:archive"
    AUTO_PASS = "auto_pass"
    ARCHIVE_QUEUED = "archive:queued"
    DOWNLOADED = "downloaded"
    UNREACHABLE = "unreachable"
    EXISTING = "existing"
    FILTERED = "filtered"
    TEXT_EXTRACTED = "text_extracted"
    OCR_DONE = "ocr_done"
    CARD_WRITTEN = "card_written"
    CHECK_PASSED = "check_passed"
    CHECK_FAILED = "check_failed"
    SEMANTIC_VERIFIED = "semantic_verified"
    SEMANTIC_FAILED = "semantic_failed"
    NOTE_WRITTEN = "note_written"
    LINKED = "linked"
    CLUSTER_SUMMARIZED = "cluster_summarized"


class FeedbackTag(enum.StrEnum):
    GOOD = "good"
    IRRELEVANT = "irrelevant"
    READ = "read"
    ASK = "ask"
    ARCHIVE = "archive"


class FeedbackChannel(enum.StrEnum):
    TELEGRAM = "telegram"
    IMAP = "imap"
    MARKDOWN = "markdown"
    CLI = "cli"


class FieldClass(enum.StrEnum):
    """Field classification used by rules and certain-pass."""

    MATH = "math"
    MATH_PHYS = "math_phys"
    PHYSICS = "physics"
    NON_PHYSICS = "non_physics"
    UNKNOWN = "unknown"


class VenueIndicator(enum.StrEnum):
    """Open-data venue quality indicator."""

    STRONG = "strong"
    NORMAL = "normal"
    WEAK = "weak"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# 4. Discovery types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """Half-open UTC time window ``[start, end)`` used by ``Source.discover``."""

    start: _dt.datetime
    end: _dt.datetime

    def __post_init__(self) -> None:
        for name in ("start", "end"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"Window.{name} must be timezone-aware")
        if self.end < self.start:
            raise ValueError("Window.end must not be before Window.start")

    @property
    def days(self) -> float:
        return (self.end - self.start).total_seconds() / 86400.0

    @classmethod
    def ending_at(cls, end: _dt.datetime, days: int | float) -> Window:
        """Window of ``days`` length that ends at ``end``."""
        return cls(start=end - _dt.timedelta(days=days), end=end)

    def contains(self, moment: _dt.datetime) -> bool:
        return self.start <= moment < self.end


class IdentKind(enum.StrEnum):
    """Record key prefixes."""

    DOI = "doi"
    ARXIV = "arxiv"
    EPRINT = "eprint"
    OPENALEX = "oa"
    ZBMATH = "zb"
    PUBMED = "pmid"
    S2 = "s2"
    RSS = "rss"
    TITLE = "nt"


_CANDIDATE_ID_PREFIX: dict[IdentKind, str] = {
    IdentKind.DOI: "doi:",
    IdentKind.ARXIV: "arXiv:",
    IdentKind.EPRINT: "ePrint:",
    IdentKind.OPENALEX: "",  # W<id>
    IdentKind.ZBMATH: "zbMATH:",
    IdentKind.PUBMED: "pmid:",
    IdentKind.S2: "s2:",
    IdentKind.RSS: "rss:",
    IdentKind.TITLE: "nt:",
}

_CANDIDATE_PREFIX_TO_KIND: dict[str, IdentKind] = {
    "doi": IdentKind.DOI,
    "arxiv": IdentKind.ARXIV,
    "eprint": IdentKind.EPRINT,
    "zbmath": IdentKind.ZBMATH,
    "pmid": IdentKind.PUBMED,
    "s2": IdentKind.S2,
    "rss": IdentKind.RSS,
    "nt": IdentKind.TITLE,
    "oa": IdentKind.OPENALEX,
}


@dataclass(frozen=True)
class Ident:
    """A typed identifier. ``key`` is the pool key (``doi:10.1/x``), ``candidate_id`` the
    message/run id form (``arXiv:2510.01234``, ``W123``, ``doi:10.1/x``)."""

    kind: IdentKind
    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("Ident.value must not be empty")

    @property
    def key(self) -> str:
        return f"{self.kind.value}:{self.value}"

    @property
    def candidate_id(self) -> str:
        if self.kind is IdentKind.OPENALEX:
            return self.value if self.value.startswith("W") else f"W{self.value}"
        return f"{_CANDIDATE_ID_PREFIX[self.kind]}{self.value}"

    @classmethod
    def parse_key(cls, key: str) -> Ident:
        """Inverse of :attr:`key`."""
        prefix, sep, value = key.partition(":")
        if not sep or not value:
            raise ValueError(f"not a record key: {key!r}")
        try:
            return cls(IdentKind(prefix), value)
        except ValueError:
            raise ValueError(f"unknown key prefix in {key!r}") from None

    @classmethod
    def parse_candidate_id(cls, cid: str) -> Ident:
        """Inverse of :attr:`candidate_id`."""
        if cid.startswith("W") and cid[1:].isdigit():
            return cls(IdentKind.OPENALEX, cid)
        prefix, sep, value = cid.partition(":")
        if not sep or not value:
            raise ValueError(f"not a candidate id: {cid!r}")
        kind = _CANDIDATE_PREFIX_TO_KIND.get(prefix.lower())
        if kind is None:
            raise ValueError(f"unknown candidate id prefix in {cid!r}")
        return cls(kind, value)


@dataclass
class Author:
    name: str
    orcid: str | None = None
    openalex_id: str | None = None


@dataclass
class Record:
    """Normalised bibliographic record produced by every source."""

    title: str
    authors: list[Author] = field(default_factory=list)
    abstract: str | None = None
    abstract_source: str | None = None
    venue: str | None = None
    venue_openalex_id: str | None = None  # OpenAlex source id (``S123``) when the source knows it
    issn: list[str] = field(default_factory=list)
    published: _dt.date | None = None
    created: _dt.date | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    eprint_id: str | None = None
    openalex_id: str | None = None
    zbmath_id: str | None = None
    pubmed_id: str | None = None
    s2_id: str | None = None
    url: str | None = None
    categories: list[str] = field(default_factory=list)
    primary_category: str | None = None
    msc: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    referenced_works: list[str] = field(default_factory=list)
    kind: WorkKind = WorkKind.UNKNOWN
    sources: set[str] = field(default_factory=set)
    queries: set[str] = field(default_factory=set)


@dataclass
class OaCandidate:
    """One open-access PDF location proposed by a source."""

    url: str
    source: str  # "arxiv" | "eprint" | "openalex" | "unpaywall" | other source name
    version: str | None = None  # e.g. arXiv "v2"
    license: str | None = None
    is_pdf: bool = True  # False: landing page that must be resolved first


@dataclass
class CheckResult:
    """Result of a small self-test (``sources test``, ``notify test``, ``model test``)."""

    ok: bool
    message: str = ""
    latency_s: float | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceTest(CheckResult):
    """``Source.test`` result: one small request."""

    source: str = ""
    key_present: bool | None = None
    http_status: int | None = None


@dataclass
class HttpResponse:
    """What ``HttpClient.get`` returns. Body is raw bytes; helpers decode lazily."""

    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    from_cache: bool = False
    elapsed_s: float = 0.0

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, errors="replace")


@dataclass
class HttpError:
    """One HTTP failure, e.g. ``"<host> HTTP 429"`` or ``"<host>: 3 tries failed"``."""

    host: str
    message: str
    status: int | None = None
    url: str | None = None


@runtime_checkable
class HttpClient(Protocol):
    """The single HTTP layer every source and archive module uses.

    Implemented by ``kallimachos.http.client.Http``. Tests pass fixture-backed fakes.
    """

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        tries: int = 3,
        timeout: float = 60.0,
        kind: str = "list",
        accept_status: Collection[int] = (),
    ) -> HttpResponse | None: ...

    @property
    def errors(self) -> list[HttpError]: ...


@runtime_checkable
class Source(Protocol):
    """Source plug-in. New source =
    ``sources/<name>.py`` + fixtures + registry entry."""

    name: str
    needs_key: bool
    default_interval_s: float

    def discover(self, window: Window, profile: Profile, http: HttpClient) -> Iterator[Record]: ...

    def metadata(self, ident: Ident, http: HttpClient) -> Record | None: ...

    def oa_pdf_candidates(self, record: Record, http: HttpClient) -> list[OaCandidate]: ...

    def test(self, http: HttpClient) -> SourceTest: ...


# ---------------------------------------------------------------------------
# 5. Rule chain, Candidate, RunResult
# ---------------------------------------------------------------------------


class RuleStage(enum.StrEnum):
    DROP = "drop"
    PASS = "pass"
    ANNOTATE = "annotate"


class VerdictKind(enum.StrEnum):
    DROP = "drop"
    PASS = "pass"
    NEUTRAL = "neutral"


@dataclass
class Verdict:
    kind: VerdictKind
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def neutral(cls) -> Verdict:
        return cls(VerdictKind.NEUTRAL)


@dataclass
class RuleTraceEntry:
    """One line of ``Candidate.rule_trace`` (no candidate disappears silently)."""

    rule: str
    verdict: VerdictKind
    reason: str = ""


@dataclass
class NearNote:
    """Why a candidate is a near miss."""

    seed: str
    note_ref: str | None = None
    match: Literal["citation", "topic"] = "topic"


@dataclass
class Candidate:
    """One scored candidate in ``runs/<run_id>.json``."""

    no: int
    id: str
    title: str
    authors: list[str] = field(default_factory=list, metadata={"maxItems": 15})
    authors_short: str = ""
    venue: str | None = None
    venue_short: str | None = None
    date: _dt.date | None = None
    url: str | None = None
    doi: str | None = None
    arxiv: str | None = None
    abstract: str | None = None
    abstract_available: bool = False
    abstract_message: str | None = None
    abstract_truncated: bool = False
    channels: list[str] = field(default_factory=list)
    channel_names: list[str] = field(default_factory=list)
    score: float = 0.0
    threshold: float = 0.0
    score_reasons: list[str] = field(default_factory=list)
    always_show_author: bool = False
    full_journal: bool = False
    quota: bool = False
    noise: bool = False
    note: str | None = None
    sources: list[str] = field(default_factory=list)
    near_note: NearNote | None = None
    field_class: FieldClass = FieldClass.UNKNOWN
    venue_quality_short: str | None = None
    quality_mark: str = ""
    certain_pass: bool = False
    certain_pass_reason: str | None = None
    rule_trace: list[RuleTraceEntry] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)  # pool keys, for seen/archive matching
    kind: WorkKind = WorkKind.UNKNOWN


@dataclass
class RuleContext:
    """Everything a rule may consult besides the candidate.

    ``exempt`` holds candidate ids for which no drop rule applies (always_show author, explicit
    user request, user "keep"). ``prior_user_drops`` holds ids the user marked irrelevant before.
    ``extra`` is the only place a module may add context without touching this file.
    """

    run_kind: RunKind
    run_date: _dt.date
    profile: Profile
    weights: Weights
    seen: Mapping[str, SeenEntry] = field(default_factory=dict)
    exempt: frozenset[str] = frozenset()
    prior_user_drops: frozenset[str] = frozenset()
    venue_indicator: Mapping[str, VenueIndicator] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Rule(Protocol):
    """One link of the rule chain."""

    id: str
    stage: RuleStage

    def apply(self, cand: Candidate, ctx: RuleContext) -> Verdict: ...


@dataclass
class SourceStats:
    """Per-source counters inside ``RunResult.sources``."""

    count: int = 0
    requests: int = 0
    errors: list[str] = field(default_factory=list)
    budget: float | None = None


@dataclass
class MessageBudget:
    """UTF-16 budget for the notification message."""

    target_utf16: int = 3600
    limit_utf16: int = 3900


@dataclass
class RunResult:
    """``runs/<run_id>.json`` (schema ``kallimachos.run/1``)."""

    run_id: str = field(metadata={"pattern": RUN_ID_PATTERN})
    kind: RunKind = RunKind.DAILY
    date: _dt.date = field(default_factory=lambda: _dt.date(1970, 1, 1))
    window: Window | None = None
    schema: Literal["kallimachos.run/1"] = "kallimachos.run/1"
    sources: dict[str, SourceStats] = field(default_factory=dict)
    pool_size: int = 0
    passed: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    dropped_examples: dict[str, list[str]] = field(default_factory=dict)  # rule -> first 5 ids
    candidates: list[Candidate] = field(default_factory=list)
    message_budget: MessageBudget = field(default_factory=MessageBudget)
    selection_rule: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    first_run: bool = False
    model_fallback: bool = False


@dataclass
class Selection:
    """``runs/<run_id>.selection.json``."""

    run_id: str = field(metadata={"pattern": RUN_ID_PATTERN})
    written_at: _dt.datetime = field(
        default_factory=lambda: _dt.datetime(1970, 1, 1, tzinfo=_dt.UTC)
    )
    by: Literal["model", "rule", "user"] = "rule"
    schema: Literal["kallimachos.selection/1"] = "kallimachos.selection/1"
    main: list[str] = field(default_factory=list)
    near: list[str] = field(default_factory=list)
    quota_not_in_main: list[str] = field(default_factory=list)
    message_no: dict[str, str] = field(default_factory=dict)  # "1" -> candidate id, "H2" -> id


# ---------------------------------------------------------------------------
# 6. Feedback, seen, weights
# ---------------------------------------------------------------------------


@dataclass
class FeedbackEntry:
    """One line of ``state/feedback.jsonl``."""

    at: _dt.datetime
    run_id: str = field(metadata={"pattern": RUN_ID_PATTERN})
    item_no: str = ""  # "3" or "H2"
    candidate_id: str = ""
    tag: FeedbackTag = FeedbackTag.GOOD
    note: str | None = None
    channel: FeedbackChannel = FeedbackChannel.CLI
    resolved_via: Literal["selection", "run_no"] = "selection"


@dataclass
class RawFeedback:
    """An unparsed user reply fetched by an intake; ``feedback.grammar`` turns it into entries."""

    at: _dt.datetime
    channel: FeedbackChannel
    text: str
    run_hint: str | None = None  # run id recovered from reply context / subject / marker
    message_ref: str | None = None  # telegram update id, IMAP message id, markdown line ref


@runtime_checkable
class FeedbackIntake(Protocol):
    """Fetches new raw replies from one channel. Idempotent via ``cursor``."""

    name: str

    def poll(self, cursor: Mapping[str, Any]) -> tuple[list[RawFeedback], dict[str, Any]]: ...


@dataclass
class SeenEntry:
    """One line of ``state/seen.jsonl``. Seen is not the same as shown."""

    keys: list[str]
    first_run: str = field(metadata={"pattern": RUN_ID_PATTERN})
    last_run: str = field(metadata={"pattern": RUN_ID_PATTERN})
    shown: bool = False
    shown_run: str | None = None
    shown_no: int | None = None
    kind: WorkKind = WorkKind.UNKNOWN
    title_short: str = ""
    expired: bool = False
    journal_version_note: str | None = (
        None  # set when the preprint later appears as journal version
    )


@dataclass
class MultWeight:
    mult: float = 1.0


@dataclass
class BonusWeight:
    bonus: float = 0.0


@dataclass
class WeightChange:
    at: _dt.datetime
    change: str
    evidence_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class Weights:
    """``weights.json`` (schema ``kallimachos.weights/1``)."""

    schema: Literal["kallimachos.weights/1"] = "kallimachos.weights/1"
    updated_at: _dt.datetime | None = None
    channel: dict[str, MultWeight] = field(default_factory=dict)
    author: dict[str, BonusWeight] = field(default_factory=dict)
    venue: dict[str, MultWeight] = field(default_factory=dict)
    phrase: dict[str, MultWeight] = field(default_factory=dict)  # "<channel>/<term>" -> mult
    history: list[WeightChange] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 7. Model provider and notifier
# ---------------------------------------------------------------------------


@runtime_checkable
class Provider(Protocol):
    """Model layer. ``complete`` returns the raw text; callers validate JSON."""

    name: str

    def complete(
        self, prompt: str, *, timeout_s: int, json_schema: dict[str, Any] | None
    ) -> str: ...

    def test(self) -> CheckResult: ...


@dataclass
class MessageItem:
    no: str  # "1".."10", "H1".. for the followed-authors section
    candidate_id: str


@dataclass
class Message:
    """A rendered notification. ``text`` is plain text within the UTF-16 budget."""

    run_id: str = field(metadata={"pattern": RUN_ID_PATTERN})
    text: str = ""
    subject: str | None = None
    html: str | None = None
    items: list[MessageItem] = field(default_factory=list)


@dataclass
class NotifyResult:
    channel: str
    ok: bool
    message_ref: str | None = None  # telegram message id, email Message-ID, markdown path
    error: str | None = None


@runtime_checkable
class Notifier(Protocol):
    """Notification channel."""

    name: str

    def send(self, message: Message) -> NotifyResult: ...

    def test(self) -> CheckResult: ...


# ---------------------------------------------------------------------------
# 8. Archive
# ---------------------------------------------------------------------------


class CatalogEventType(enum.StrEnum):
    STORED = "stored"
    EXISTING = "existing"
    REMOVED = "removed"
    REPLACED = "replaced"
    RENAMED = "renamed"
    MOVED = "moved"
    SUPERSEDED = "superseded"
    METADATA_FIXED = "metadata_fixed"
    ZOTERO_LINKED = "zotero_linked"
    IN_BOOK = "in_book"
    OUTGOING = "outgoing"
    DELIVERED = "delivered"
    MERGED = "merged"
    USER_SUPPLIED = "user_supplied"


class CatalogStatus(enum.StrEnum):
    STORED = "stored"
    EXISTING = "existing"
    REMOVED = "removed"
    IN_BOOK = "in_book"
    OUTGOING = "outgoing"  # bridge: waiting for delivery


@dataclass
class CatalogEvent:
    at: _dt.datetime
    type: CatalogEventType
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ArchiveTrigger:
    """Why a work entered the archive queue."""

    kind: Literal["feedback", "auto_pass", "always_show_new", "manual"]
    ident: str  # candidate id or pool key
    run_id: str | None = None
    channel_hint: str | None = None
    person_folder: str | None = None
    note: str | None = None


@dataclass
class CatalogEntry:
    """One line of ``archive/catalog.jsonl``. ``id`` is the eid once downloaded."""

    id: str
    keys: list[str] = field(default_factory=list)
    doi: str | None = None
    arxiv: str | None = None
    arxiv_version: str | None = None
    eprint: str | None = None
    openalex_id: str | None = None
    title: str = ""
    authors: list[str] = field(default_factory=list, metadata={"maxItems": 30})
    year: int | None = None
    venue: str | None = None
    kind: WorkKind = WorkKind.UNKNOWN
    channel: str | None = None
    folder: str | None = None
    path: str | None = field(  # relative to archive.root; never absolute, never a ".." component
        default=None, metadata={"pattern": CATALOG_PATH_PATTERN}
    )
    filename: str | None = None
    sha256: str | None = field(default=None, metadata={"pattern": "^[0-9a-f]{64}$"})
    size: int | None = None
    pages: int | None = None
    oa_source: str | None = None
    oa_url: str | None = None
    downloaded_at: _dt.datetime | None = None
    status: CatalogStatus = CatalogStatus.STORED
    triggers: list[str] = field(default_factory=list)
    events: list[CatalogEvent] = field(default_factory=list)
    zotero_key: str | None = None


@dataclass
class RetryAttempt:
    at: _dt.datetime
    result: str


@dataclass
class UnreachableEntry:
    """One line of ``archive/unreachable.jsonl``."""

    id: str
    keys: list[str] = field(default_factory=list)
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi_url: str | None = None
    landing: str | None = None
    venue: str | None = None
    folder: str | None = None  # channel folder the archive would use (bridge ``_Incoming``)
    filename: str | None = None  # file name the archive would use
    reason: str = ""
    oa_tried: list[str] = field(default_factory=list)
    status: Literal["open", "closed", "dropped"] = "open"
    closed_by: str | None = None
    retries: list[RetryAttempt] = field(default_factory=list)


@dataclass
class ProcessedEntry:
    """One line of ``archive/processed.jsonl``: a trigger the archive decided not to download."""

    at: _dt.datetime
    keys: list[str]
    status: Literal["filtered", "no_channel", "unsupported", "duplicate", "superseded_skip"]
    reason: str = ""
    trigger: str = ""


@dataclass
class ExistingEntry:
    """One line of ``archive/existing_catalog.jsonl``."""

    path: str  # relative to ``root``
    root: str
    size: int
    mtime: float
    local: bool = True  # False: cloud placeholder (dataless), never opened
    sha256: str | None = field(default=None, metadata={"pattern": "^[0-9a-f]{64}$"})
    doi: str | None = None
    arxiv: str | None = None
    arxiv_version: str | None = None
    eprint: str | None = None
    pii: str | None = None
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    kind_guess: WorkKind = WorkKind.UNKNOWN
    keys: list[str] = field(default_factory=list)
    identified_by: list[str] = field(default_factory=list)  # filename | text | pdfinfo | lookup
    queried: bool = False  # True only when the privacy gate passed and a lookup ran
    extractor_version: int = 1


@dataclass
class ArchiveLogLine:
    """One line of ``archive/log.jsonl``: a run summary with counts only (no titles/names)."""

    at: _dt.datetime
    command: str
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 9. Processing
# ---------------------------------------------------------------------------


@dataclass
class TextMeta:
    """``process/text/<eid>.meta.json``."""

    eid: str = field(metadata={"pattern": EID_PATTERN})
    path: str = ""
    pages: int = 0
    chars: int = 0
    ocr: Literal["none", "needed", "done"] = "none"
    alpha_ratio: float = 0.0
    spaced_ratio: float = 0.0
    note: list[str] = field(default_factory=list)


class ItemType(enum.StrEnum):
    DEFINITION = "definition"
    THEOREM = "theorem"
    PROPOSITION = "proposition"
    LEMMA = "lemma"
    COROLLARY = "corollary"
    CONSTRUCTION = "construction"
    EXAMPLE = "example"
    CONJECTURE = "conjecture"
    OPEN_PROBLEM = "open_problem"
    QUESTION = "question"
    CLAIMED_SOLUTION = "claimed_solution"
    METHOD = "method"
    REMARK = "remark"
    RESEARCH_DIRECTION = "research_direction"
    ALGORITHM = "algorithm"


class ItemStatus(enum.StrEnum):
    OPEN = "open"
    SOLVED_HERE = "solved_here"
    PARTLY_SOLVED_HERE = "partly_solved_here"
    SOLVED_ELSEWHERE = "solved_elsewhere"


@dataclass
class CardCitation:
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    year_catalog: int | None = None
    year_pdf: int | None = None
    year_note: str | None = None
    title: str = ""
    venue: str | None = None
    doi: str | None = None
    arxiv: str | None = None
    page_range: str | None = None


@dataclass
class CardItem:
    """One extracted item of a card. ``quote`` is verbatim, 40-400 chars."""

    type: ItemType
    label: str
    page: int
    quote: str = field(metadata={"minLength": 40, "maxLength": 400})
    statement: str
    number: str | None = None
    printed_page: str | None = None
    concepts: list[str] = field(default_factory=list)
    cites: list[str] = field(default_factory=list)
    importance: Literal["main", "supporting"] = "main"
    quoted_from_other: bool = False
    status: ItemStatus | None = None
    solution_cites: list[str] | None = None
    concreteness: Literal["concrete", "open_ended"] | None = None
    status_note: str | None = None
    solution_kind: str | None = None
    target: str | None = None


@dataclass
class CardReference:
    key: str
    text: str
    doi: str | None = None
    arxiv: str | None = None


@dataclass
class CardFlags:
    struggled: bool = False
    potential: Literal["low", "medium", "high"] = "medium"
    rationale: str = ""
    ocr_weak: bool = False
    unreadable_pages: list[int] = field(default_factory=list)
    formulas_unreliable: bool = False


@dataclass
class FalseAlarm:
    page: int
    phrase: str
    reason: str


@dataclass
class Card:
    """``process/cards/<eid>.json`` (schema ``kallimachos.card/4``)."""

    eid: str = field(metadata={"pattern": EID_PATTERN})
    path: str = ""
    pages: int = 0
    schema: Literal["kallimachos.card/4"] = "kallimachos.card/4"
    citation: CardCitation = field(default_factory=CardCitation)
    summary: str = ""
    items: list[CardItem] = field(default_factory=list)
    references: list[CardReference] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)
    flags: CardFlags = field(default_factory=CardFlags)
    false_alarms: list[FalseAlarm] = field(default_factory=list)


@dataclass
class Mismatch:
    """One semantic mismatch: a missing problem/claim, b missing main result,
    c number/page mismatch, d lost qualifier/assumption."""

    kind: Literal["a", "b", "c", "d"]
    page: int | None = None
    quote: str = ""
    note_sentence: str = ""
    severity: Literal["minor", "major"] = "major"


@dataclass
class SemanticResult:
    """``process/semantic/<eid>.json`` (schema ``kallimachos.semantic_result/1``)."""

    eid: str = field(metadata={"pattern": EID_PATTERN})
    verifier_model: str = ""
    at: _dt.datetime = field(default_factory=lambda: _dt.datetime(1970, 1, 1, tzinfo=_dt.UTC))
    result: Literal["pass", "fail"] = "fail"
    schema: Literal["kallimachos.semantic_result/1"] = "kallimachos.semantic_result/1"
    mismatches: list[Mismatch] = field(default_factory=list)
    pages_read_from_image: list[int] = field(default_factory=list)


@dataclass
class NoteFrontmatter:
    """Frontmatter written by ``process note-write`` (contract ``note/1``).

    Only these keys plus ``extra`` are ever written; Avenox type-checks kind/project/visibility/
    updated_at and leaves the rest as free metadata.
    """

    eid: str = field(metadata={"pattern": EID_PATTERN})
    project: str = ""
    channel: str = ""
    reader: str = ""
    read_at: _dt.date = field(default_factory=lambda: _dt.date(1970, 1, 1))
    updated_at: _dt.date = field(default_factory=lambda: _dt.date(1970, 1, 1))
    source_pdf: str = ""
    kind: str = "note"
    visibility: Literal["public", "internal", "private"] = "internal"
    zotero: str | None = None
    citekey: str | None = None
    kallimachos: Literal["note/1"] = "note/1"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClusterFrontmatter:
    """Frontmatter of ``_Cluster-Summary.md`` (contract ``cluster/1``)."""

    cluster: str
    project: str = ""
    work_count: int = 0
    updated_at: _dt.date = field(default_factory=lambda: _dt.date(1970, 1, 1))
    kind: str = "note"
    visibility: Literal["public", "internal", "private"] = "internal"
    kallimachos: Literal["cluster/1"] = "cluster/1"


@dataclass
class QueueSync:
    """How a reading-queue entry was delivered: an Avenox task id or a line in the queue file."""

    avenox_task_id: str | None = None
    queue_file_line: int | None = None


@dataclass
class ReadingQueueEntry:
    """One line of ``process/reading_queue.jsonl``."""

    at: _dt.datetime
    kind: Literal["read", "ask"]
    title: str = ""
    candidate_id: str | None = None
    eid: str | None = field(default=None, metadata={"pattern": EID_PATTERN})
    url: str | None = None
    note: str | None = None
    synced: QueueSync | None = None


@dataclass
class StatusEntry:
    """One append-only line of ``process/status.jsonl``. Last line per eid wins."""

    eid: str = field(metadata={"pattern": EID_PATTERN})
    state: WorkState = WorkState.DOWNLOADED
    at: _dt.datetime = field(default_factory=lambda: _dt.datetime(1970, 1, 1, tzinfo=_dt.UTC))
    by: str = ""  # CLI command that wrote the transition


@dataclass
class GraphEdge:
    """One line of ``process/graph.jsonl`` / ``graph_uncertain.jsonl``."""

    source_eid: str = field(metadata={"pattern": EID_PATTERN})
    target_eid: str = field(metadata={"pattern": EID_PATTERN})
    key: str = ""
    method: Literal["doi", "arxiv", "title", "author_year_title"] = "title"
    score: float = 0.0
    text: str = ""


# ---------------------------------------------------------------------------
# Schema registry: file name under schemas/ -> (dataclass, $id)
# ---------------------------------------------------------------------------

_SCHEMA_BASE = "https://github.com/oselcukk/kallimachos/schemas/"

SCHEMA_REGISTRY: dict[str, tuple[type[Any], str]] = {
    "run.schema.json": (RunResult, _SCHEMA_BASE + "run/1"),
    "selection.schema.json": (Selection, _SCHEMA_BASE + "selection/1"),
    "feedback.schema.json": (FeedbackEntry, _SCHEMA_BASE + "feedback/1"),
    "seen.schema.json": (SeenEntry, _SCHEMA_BASE + "seen/1"),
    "weights.schema.json": (Weights, _SCHEMA_BASE + "weights/1"),
    "catalog.schema.json": (CatalogEntry, _SCHEMA_BASE + "catalog/1"),
    "unreachable.schema.json": (UnreachableEntry, _SCHEMA_BASE + "unreachable/1"),
    "processed.schema.json": (ProcessedEntry, _SCHEMA_BASE + "processed/1"),
    "existing_catalog.schema.json": (ExistingEntry, _SCHEMA_BASE + "existing_catalog/1"),
    "archive_log.schema.json": (ArchiveLogLine, _SCHEMA_BASE + "archive_log/1"),
    "text_meta.schema.json": (TextMeta, _SCHEMA_BASE + "text_meta/1"),
    "card.schema.json": (Card, _SCHEMA_BASE + "card/4"),
    "semantic_result.schema.json": (SemanticResult, _SCHEMA_BASE + "semantic_result/1"),
    "note_frontmatter.schema.json": (NoteFrontmatter, _SCHEMA_BASE + "note/1"),
    "cluster_frontmatter.schema.json": (ClusterFrontmatter, _SCHEMA_BASE + "cluster/1"),
    "reading_queue.schema.json": (ReadingQueueEntry, _SCHEMA_BASE + "reading_queue/1"),
    "status.schema.json": (StatusEntry, _SCHEMA_BASE + "status/1"),
    "graph_edge.schema.json": (GraphEdge, _SCHEMA_BASE + "graph_edge/1"),
}
"""Config and profile schemas are registered by ``config.schema`` / ``profile.schema`` (see
``kallimachos.config.schema.register_schemas``) to avoid an import cycle."""
