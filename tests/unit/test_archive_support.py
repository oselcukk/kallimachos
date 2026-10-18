"""Shared fakes for the archive tests. No tests here; imported by test_archive_*.py.

* :class:`FakeHttp` is the shared ``FixtureHttp`` with a URL -> response ``add`` and a URL log;
* :class:`FakeSource` implements the ``Source`` protocol from canned records/candidates;
* :func:`make_pdf` builds a small valid synthetic PDF (no real papers);
* ``store_impl`` falls back to a minimal ``state.store`` when the real store is unavailable, and
  uses the real store otherwise.
"""

from __future__ import annotations

import json
import os
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from kallimachos.interfaces import (
    HttpResponse,
    Ident,
    OaCandidate,
    Record,
    SourceTest,
    Window,
)
from kallimachos.state import store
from tests.unit.fakes import FixtureHttp


def make_pdf(text: str = "Synthetic test document") -> bytes:
    """A one-page PDF with ``text`` drawn in Helvetica; offsets in the xref table are exact."""
    safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


class FakeHttp(FixtureHttp):
    """Archive view of the shared :class:`tests.unit.fakes.FixtureHttp`: ``add(url, body)`` with
    a content type, unrouted URLs answer ``None`` + error, ``calls`` lists the URLs."""

    def __init__(self) -> None:
        super().__init__(strict=False)

    def add(  # type: ignore[override]
        self, url: str, body: bytes | str, *, status: int = 200, ctype: str = "application/pdf"
    ) -> None:
        self.add_prefix(
            url,
            HttpResponse(
                url=url,
                status=status,
                headers={"Content-Type": ctype},
                body=body.encode() if isinstance(body, str) else body,
            ),
        )

    @property
    def calls(self) -> list[str]:
        return [r.url for r in self.requests]


@dataclass
class FakeSource:
    name: str
    records: dict[str, Record] = field(default_factory=dict)  # ident.key -> record
    candidates: list[OaCandidate] = field(default_factory=list)
    needs_key: bool = False
    default_interval_s: float = 1.0
    metadata_calls: list[str] = field(default_factory=list)
    oa_calls: int = 0

    def discover(self, window: Window, profile: Any, http: Any) -> Iterator[Record]:
        return iter(())

    def metadata(self, ident: Ident, http: Any) -> Record | None:
        self.metadata_calls.append(ident.key)
        return self.records.get(ident.key)

    def oa_pdf_candidates(self, record: Record, http: Any) -> list[OaCandidate]:
        self.oa_calls += 1
        return list(self.candidates)

    def test(self, http: Any) -> SourceTest:
        return SourceTest(ok=True, source=self.name)


# ---------------------------------------------------------------------------
# minimal state.store used only when the real implementation is unavailable
# ---------------------------------------------------------------------------


def _nfc(data: Any) -> Any:
    if isinstance(data, str):
        return unicodedata.normalize("NFC", data)
    if isinstance(data, list):
        return [_nfc(v) for v in data]
    if isinstance(data, dict):
        return {_nfc(k): _nfc(v) for k, v in data.items()}
    return data


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(unicodedata.normalize("NFC", text), encoding="utf-8")
    os.replace(tmp, path)


def _append_jsonl(path: Path, record: Mapping[str, Any], *, schema: Any = None) -> None:
    if schema is not None:
        import jsonschema

        jsonschema.validate(dict(record), schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_nfc(dict(record)), ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def _read_json(path: Path, default: Any = None) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write_json_atomic(path: Path, data: Any, *, schema: Any = None) -> None:
    _write_text_atomic(path, json.dumps(_nfc(data), ensure_ascii=False, indent=2))


def _l1_store_ready(tmp: Path) -> bool:
    try:
        store.read_json(tmp / "missing.json", default=None)
    except NotImplementedError:
        return False
    return True


@pytest.fixture
def store_impl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if _l1_store_ready(tmp_path):
        return
    monkeypatch.setattr(store, "write_text_atomic", _write_text_atomic)
    monkeypatch.setattr(store, "append_jsonl", _append_jsonl)
    monkeypatch.setattr(store, "read_jsonl", _read_jsonl)
    monkeypatch.setattr(store, "read_json", _read_json)
    monkeypatch.setattr(store, "write_json_atomic", _write_json_atomic)
