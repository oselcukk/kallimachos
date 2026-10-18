"""Shared helpers for the processing tests (``test_process_core_*``, ``test_zotero_*``).

* :func:`make_pdf` builds a small synthetic PDF (fictional text, Helvetica) so ``pdftotext`` can
  be exercised when it is installed; the extraction logic is otherwise tested through a fake
  runner.
* :func:`fake_store` replaces ``kallimachos.state.store`` with a minimal
  implementation that keeps the store contract (atomic write, NFC, schema validation).
* :func:`fake_config` makes ``config.load.load_config`` return defaults rooted in
  the isolated test home.

Import the fixtures into a test module::

    from tests.unit.conftest_pdf import fake_config_fixture, fake_store  # noqa: F401

(``layout``/``config``/``fake_config`` are the fixture names; the imported helpers end in
``_fixture`` so a test parameter named ``layout`` is not a redefinition for the linter.)
"""

from __future__ import annotations

import json
import os
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from kallimachos.archive.catalog import Catalog
from kallimachos.config.schema import Config
from kallimachos.interfaces import (
    Card,
    CardCitation,
    CardItem,
    CardReference,
    CatalogEntry,
    ItemType,
    WorkKind,
)
from kallimachos.paths import DataLayout

EID_A = "0123456789ab"
EID_B = "abcdef012345"
EID_C = "fedcba987654"


def sha_for(eid: str) -> str:
    """A 64-hex sha256 whose first 12 characters are ``eid``."""
    return (eid + "0" * 64)[:64]


# --- synthetic PDF ---------------------------------------------------------------------------


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(path: Path, pages: Sequence[Sequence[str] | str]) -> Path:
    """Write a minimal PDF with one Helvetica text block per page (ASCII lines only)."""
    objects: list[bytes] = []

    def add(obj: str) -> int:
        objects.append(obj.encode("latin-1"))
        return len(objects)

    font = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []
    pages_obj_index = len(objects) + 1 + 2 * len(pages)  # reserved after pages + contents
    for page in pages:
        lines = page.split("\n") if isinstance(page, str) else list(page)
        ops = ["BT", "/F1 11 Tf", "14 TL", "50 780 Td"]
        for line in lines:
            ops.append(f"({_pdf_escape(line)}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops)
        content = add(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        page_id = add(
            f"<< /Type /Page /Parent {pages_obj_index} 0 R /MediaBox [0 0 595 842] "
            f"/Contents {content} 0 R /Resources << /Font << /F1 {font} 0 R >> >> >>"
        )
        page_ids.append(page_id)
    kids = " ".join(f"{i} 0 R" for i in page_ids)
    pages_id = add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>")
    assert pages_id == pages_obj_index
    catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    trailer = f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\n"
    out += (trailer + f"startxref\n{xref}\n%%EOF\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


def fake_runner(pages: Sequence[str]) -> Any:
    """A ``Runner`` that pretends ``pdftotext`` produced ``pages`` (form feed after each page)."""
    calls: list[list[str]] = []

    def run(cmd: Sequence[str]) -> str:
        calls.append(list(cmd))
        return "".join(p + "\f" for p in pages)

    run.calls = calls  # type: ignore[attr-defined]
    return run


# --- fake state.store (store contract) ------------------------------------------------------


def _nfc(data: Any) -> Any:
    if isinstance(data, str):
        return unicodedata.normalize("NFC", data)
    if isinstance(data, Mapping):
        return {_nfc(k): _nfc(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_nfc(v) for v in data]
    return data


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _append_jsonl(path: Path, record: Mapping[str, Any], *, schema: Any = None) -> None:
    data = _nfc(dict(record))
    if schema is not None:
        jsonschema.validate(data, schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(data, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def _write_json_atomic(path: Path, data: Any, *, schema: Any = None) -> None:
    data = _nfc(data)
    if schema is not None:
        jsonschema.validate(data, schema)
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_text_atomic(path: Path, text: str) -> None:
    _atomic_write(path, unicodedata.normalize("NFC", text))


@pytest.fixture(autouse=True)
def fake_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kept for the fixture name only: the process tests run on the real ``state.store``."""


# --- config / layout -------------------------------------------------------------------------


@pytest.fixture(name="layout")
def layout_fixture(_isolated_home: Path) -> DataLayout:
    return DataLayout(_isolated_home)


@pytest.fixture(name="config")
def config_fixture() -> Config:
    return Config()


@pytest.fixture(name="fake_config")
def fake_config_fixture(
    monkeypatch: pytest.MonkeyPatch, layout: DataLayout, config: Config
) -> Config:
    """``config.load.load_config`` returns ``(config, layout)`` regardless of arguments."""
    from kallimachos.config import load as config_load

    def _load(
        path: Path | None = None, *, data_dir: str | None = None
    ) -> tuple[Config, DataLayout]:
        return config, layout

    monkeypatch.setattr(config_load, "load_config", _load)
    return config


# --- catalog and cards -----------------------------------------------------------------------


class FakeCatalog(Catalog):
    """A ``Catalog`` with entries set directly; ``load`` is not used."""

    def __init__(self, entries: Sequence[CatalogEntry], path: Path | None = None) -> None:
        super().__init__(path or Path("catalog.jsonl"))
        self._entries = list(entries)

    def get(self, entry_id: str) -> CatalogEntry | None:
        return next((e for e in self._entries if e.id == entry_id), None)


def make_entry(
    eid: str,
    *,
    title: str = "An illustrative result on fictional lattices",
    authors: Sequence[str] = ("Example Author A", "Example Author B"),
    year: int | None = 2019,
    path: str = "A-Example-Channel/01-Sub/Author_2019_Illustrative.pdf",
    channel: str = "A",
    kind: WorkKind = WorkKind.ARTICLE,
    pages: int | None = 12,
    doi: str | None = None,
    arxiv: str | None = None,
    zotero_key: str | None = None,
) -> CatalogEntry:
    return CatalogEntry(
        id=f"doi:10.0000/example.{eid[:4]}",
        title=title,
        authors=list(authors),
        year=year,
        kind=kind,
        channel=channel,
        folder="/".join(Path(path).parts[:-1]),
        path=path,
        filename=Path(path).name,
        sha256=sha_for(eid),
        pages=pages,
        doi=doi,
        arxiv=arxiv,
        zotero_key=zotero_key,
    )


QUOTE_OK = "For every fictional parameter the statement holds in the illustrative lattice."


def make_card(eid: str = EID_A, *, items: Sequence[CardItem] | None = None, **kw: Any) -> Card:
    if items is None:
        items = [
            CardItem(
                type=ItemType.THEOREM,
                label="Theorem 1.1",
                number="1.1",
                page=1,
                quote=QUOTE_OK,
                statement="For every fictional parameter the statement holds.",
            )
        ]
    refs = kw.pop(
        "references",
        [CardReference(key="[1]", text="Example Author B, Another fictional paper, 2017.")],
    )
    return Card(
        eid=eid,
        path="A-Example-Channel/01-Sub/Author_2019_Illustrative.pdf",
        pages=kw.pop("pages", 2),
        citation=kw.pop(
            "citation",
            CardCitation(authors=["Example Author A"], year=2019, title="An illustrative result"),
        ),
        summary="Fictional summary.",
        items=list(items),
        references=refs,
        **kw,
    )


PAGE_1 = (
    "An illustrative result\n\nExample Author A\n\n1. Introduction\n\n"
    "Theorem 1.1. " + QUOTE_OK + "\n\nProof. Trivial in the fictional setting.\n"
)
PAGE_2 = "References\n\n[1] Example Author B, Another fictional paper, 2017.\n"
