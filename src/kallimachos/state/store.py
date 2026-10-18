"""Atomic JSON / JSONL persistence.

Rules every implementation must keep:

* one writer API for the whole program: :func:`append_jsonl`, :func:`write_jsonl_atomic` and
  :func:`write_json_atomic`;
* atomic writes: temp file in the same directory + ``os.replace``; every file is created
  owner-only (``0600``: ``mkstemp`` for the atomic writers, ``O_CREAT`` with ``0600`` for the
  appender), the directories ``0700`` (:meth:`kallimachos.paths.DataLayout.ensure_private_dirs`);
* every string is NFC-normalised on write (``records.normalize.nfc``);
* JSON is UTF-8, ``ensure_ascii=False``, keys in insertion order, one object per JSONL line;
* when ``schema`` is given the object is validated (``jsonschema``) before it is written and a
  :class:`~kallimachos.interfaces.SchemaViolation` is raised on failure.

Values that are not plain JSON (dataclasses, enums, dates, sets) are converted with
:func:`kallimachos.interfaces.dump` first, so callers may pass shared records directly.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema

from kallimachos.interfaces import SCHEMA_REGISTRY, SchemaViolation, dump, json_schema
from kallimachos.records.normalize import nfc


def normalize_for_storage(data: Any) -> Any:
    """Recursively NFC-normalise every string in ``data`` (dicts, lists, scalars).

    Dataclasses, enums, dates and sets are converted to plain JSON values on the way.
    """
    if isinstance(data, str):
        return nfc(data)
    if isinstance(data, Mapping):
        return {nfc(str(k)): normalize_for_storage(v) for k, v in data.items()}
    if isinstance(data, list | tuple):
        return [normalize_for_storage(v) for v in data]
    if data is None or isinstance(data, bool | int | float):
        return data
    # dataclasses, enums, dates, sets -> plain JSON first
    return normalize_for_storage(dump(data))


@cache
def schema_for(filename: str) -> dict[str, Any]:
    """JSON Schema of ``schemas/<filename>`` generated from the registered dataclass.

    The same generator writes the files under ``schemas/`` (``tools/gen_schemas.py``), so the
    in-memory schema and the published file never disagree (``tests/unit/test_schemas.py``).
    """
    if filename not in SCHEMA_REGISTRY:
        # config/profile schemas register themselves on import
        import kallimachos.config.schema
        import kallimachos.profile.schema  # noqa: F401
    try:
        cls, schema_id = SCHEMA_REGISTRY[filename]
    except KeyError:
        raise SchemaViolation(f"unknown schema {filename!r}") from None
    return json_schema(cls, schema_id=schema_id)


def validate(data: Any, schema: Mapping[str, Any]) -> None:
    """Validate plain data against a JSON Schema; raise :class:`SchemaViolation` with the dotted
    path of the first (most relevant) error."""
    validator_cls = jsonschema.validators.validator_for(schema)
    validator = validator_cls(schema)
    error = jsonschema.exceptions.best_match(validator.iter_errors(data))
    if error is not None:
        path = ".".join(str(p) for p in error.absolute_path)
        raise SchemaViolation(error.message, path)


def _prepare(data: Any, schema: Mapping[str, Any] | None) -> Any:
    plain = normalize_for_storage(data)
    if schema is not None:
        validate(plain, schema)
    return plain


def _dumps_line(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def append_jsonl(
    path: Path, record: Mapping[str, Any], *, schema: Mapping[str, Any] | None = None
) -> None:
    """Append one JSON object as a line, creating parent directories.

    The line is written with a single ``write`` call on a file opened in append mode, so a
    concurrent reader never sees half a line on POSIX; callers that need exclusivity hold a
    :class:`~kallimachos.state.lock.FileLock`.
    """
    plain = _prepare(record, schema)
    if not isinstance(plain, dict):
        raise SchemaViolation("a JSONL line must be a JSON object")
    path.parent.mkdir(parents=True, exist_ok=True)
    line = _dumps_line(plain) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)  # owner-only like the store
    with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def write_jsonl_atomic(
    path: Path, records: Iterable[Any], *, schema: Mapping[str, Any] | None = None
) -> None:
    """Rewrite a whole JSONL file atomically (tmp + ``os.replace``): one object per line, NFC,
    every line validated against ``schema`` when given. The one full-rewrite API for
    ``catalog.jsonl``, ``seen.jsonl``, ``graph.jsonl`` and the other state files."""
    lines: list[str] = []
    for record in records:
        plain = _prepare(record, schema)
        if not isinstance(plain, dict):
            raise SchemaViolation("a JSONL line must be a JSON object")
        lines.append(_dumps_line(plain))
    text = "".join(line + "\n" for line in lines)
    _atomic_write_bytes(path, text.encode("utf-8"))


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield objects line by line; a missing file yields nothing; a corrupt line raises
    :class:`~kallimachos.interfaces.SchemaViolation` with the line number."""
    if not path.exists():
        return
    with open(path, encoding="utf-8") as fh:
        for no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SchemaViolation(f"corrupt JSON ({exc.msg})", f"{path.name}:{no}") from None
            if not isinstance(obj, dict):
                raise SchemaViolation("line is not a JSON object", f"{path.name}:{no}")
            yield obj


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def write_json_atomic(path: Path, data: Any, *, schema: Mapping[str, Any] | None = None) -> None:
    """Write ``data`` as pretty JSON atomically (tmp + ``os.replace``)."""
    plain = _prepare(data, schema)
    text = json.dumps(plain, ensure_ascii=False, indent=2) + "\n"
    _atomic_write_bytes(path, text.encode("utf-8"))


def read_json(path: Path, default: Any = None) -> Any:
    """Read a JSON file; return ``default`` when the file is missing.

    A file that exists but is not valid JSON raises :class:`SchemaViolation`.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SchemaViolation(f"corrupt JSON ({exc.msg} at line {exc.lineno})", path.name) from None


def backup_copy(path: Path, *, label: str = "bak") -> Path | None:
    """Copy ``path`` to ``<name>.<label>-<timestamp>`` next to it before it is changed. The name
    is made unique with ``O_EXCL`` (``-1``, ``-2`` ... on a clash), so an earlier backup is never
    overwritten. Returns the backup path, or ``None`` when ``path`` does not exist. A symlink is
    followed: the backup sits next to the real file."""
    import datetime as _dt
    import shutil

    if not path.exists():
        return None
    real = Path(os.path.realpath(path))
    stamp = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    base = real.with_name(f"{real.name}.{label}-{stamp}")
    for n in range(0, 1000):
        dest = base if n == 0 else base.with_name(f"{base.name}-{n}")
        try:
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as out, open(real, "rb") as src:
            shutil.copyfileobj(src, out)
        shutil.copystat(real, dest)
        return dest
    raise OSError(f"could not find a free backup name for {path}")


def write_text_atomic(path: Path, text: str) -> None:
    """Atomic UTF-8 text write with NFC normalisation; used for Markdown files too."""
    _atomic_write_bytes(path, nfc(text).encode("utf-8"))
