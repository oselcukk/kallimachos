"""``zotero ris`` (per-category RIS, Surname, Given, L1, KW, N1 eid, --only-new), ``zotero link``
(eid matching, two frontmatter fields, marked child note, idempotent), ``fix-metadata`` (Crossref
by DOI, title+author query, strong match only, JSONL report with escaping)."""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

import pytest

from kallimachos.config.schema import ZoteroConfig
from kallimachos.interfaces import CardCitation, WorkKind
from kallimachos.paths import DataLayout
from kallimachos.process import note as note_mod
from kallimachos.process.zotero import NOTE_MARK, ZoteroClient
from kallimachos.process.zotero import link as link_mod
from kallimachos.process.zotero import metadata as meta_mod
from kallimachos.process.zotero import ris as ris_mod
from kallimachos.state import store
from tests.unit.conftest_pdf import (  # noqa: F401
    EID_A,
    EID_B,
    fake_store,
    layout_fixture,
    make_card,
    make_entry,
)
from tests.unit.test_zotero_client import BASE, CFG, FakeHttp, FakeWriter, item

# --- ris ---------------------------------------------------------------------------------------


def test_catalog_to_ris_fields_and_categories() -> None:
    entries = [
        make_entry(EID_A, authors=["Example Alpha", "Beta, Example"], doi="10.0000/example.a"),
        make_entry(
            EID_B,
            path="B-Other/02-Sub/thesis.pdf",
            kind=WorkKind.THESIS,
            channel="B",
            zotero_key="ZK1",
            arxiv="2001.00001",
        ),
    ]
    cards = {
        EID_A: make_card(
            EID_A, citation=CardCitation(authors=["X"], year=2019, title="T", page_range="1-20")
        )
    }
    out = ris_mod.catalog_to_ris(entries, cards, root=Path("/archive"))
    assert set(out) == {"A-Example-Channel", "B-Other"}
    a = out["A-Example-Channel"]
    assert "TY  - JOUR" in a
    assert "AU  - Alpha, Example" in a and "AU  - Beta, Example" in a
    assert "L1  - /archive/A-Example-Channel/01-Sub/Author_2019_Illustrative.pdf" in a
    assert "KW  - A-Example-Channel/01-Sub" in a
    assert f"N1  - eid:{EID_A}" in a
    assert "DO  - 10.0000/example.a" in a and "SP  - 1-20" in a and "PY  - 2019" in a
    assert a.rstrip().endswith("ER  -")
    b = out["B-Other"]
    assert "TY  - THES" in b and "UR  - https://arxiv.org/abs/2001.00001" in b
    only_new = ris_mod.catalog_to_ris(entries, cards, only_new=True)
    assert set(only_new) == {"A-Example-Channel"}


def test_ris_title_is_single_line() -> None:
    entry = make_entry(EID_A, title="A title\nbroken  over lines")
    assert "TI  - A title broken over lines" in ris_mod.entry_to_ris(entry, None)


# --- link ----------------------------------------------------------------------------------------


def _fake_update_frontmatter(text: str, fields: dict[str, str]) -> str:
    head, _sep, body = text.partition("\n---\n")
    lines = [ln for ln in head.splitlines() if not any(ln.startswith(f"{k}:") for k in fields)]
    lines += [f"{k}: {v}" for k, v in fields.items()]
    return "\n".join(lines) + "\n---\n" + body


@pytest.fixture
def linked_env(
    layout: DataLayout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, ZoteroClient, FakeWriter]:
    monkeypatch.setattr(note_mod, "update_frontmatter_fields", _fake_update_frontmatter)
    vault = tmp_path / "vault"
    note = vault / "Literature" / "A" / "Author-2019-Illustrative.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\nkind: note\n---\n# Body\n", encoding="utf-8")
    store.write_json_atomic(
        layout.notes_index_file,
        {
            EID_A: {
                "path": "Literature/A/Author-2019-Illustrative.md",
                "title": "T",
                "updated_at": "2026-10-18",
            },
            EID_B: {"path": "Literature/A/Missing.md", "title": "M", "updated_at": "2026-10-18"},
            "cccccccccccc": {
                "path": "Literature/A/NoItem.md",
                "title": "N",
                "updated_at": "2026-10-18",
            },
        },
    )
    writer = FakeWriter()
    http = FakeHttp(
        {
            f"{BASE}/items?format=json&limit=100&start=0": [
                item("ITEM0001", extra=f"eid: {EID_A}", citationKey="author2019illustrative"),
                item("ITEM0002", extra=f"eid: {EID_B}"),
            ]
        }
    )
    return vault, ZoteroClient(CFG, http, writer=writer), writer


def test_link_notes_writes_two_fields_and_marked_note(
    linked_env: tuple[Path, ZoteroClient, FakeWriter],
) -> None:
    vault, client, writer = linked_env
    report = link_mod.link_notes(
        client,
        _layout_of(vault),
        vault_name="Vault",
        link_style="wikilink",
        dry_run=False,
        notes_root=vault,
    )
    by_eid = {r["eid"]: r for r in report}
    assert by_eid[EID_A]["status"] == "linked"
    assert by_eid[EID_B]["status"] == "note_missing"
    assert by_eid["cccccccccccc"]["status"] == "no_item"
    text = (vault / "Literature" / "A" / "Author-2019-Illustrative.md").read_text(encoding="utf-8")
    assert "zotero: zotero://select/library/items/ITEM0001" in text
    assert "citekey: author2019illustrative" in text
    assert "kind: note" in text and text.endswith("# Body\n")
    assert len(writer.posts) == 1
    note_payload = writer.posts[0][3][0]
    assert note_payload["parentItem"] == "ITEM0001"
    assert NOTE_MARK in note_payload["note"]
    assert (
        "obsidian://open?vault=Vault&amp;file=Literature/A/Author-2019-Illustrative"
        in note_payload["note"]
    )


def _layout_of(vault: Path) -> DataLayout:
    import os

    return DataLayout(Path(os.environ["KALLIMACHOS_HOME"]))


def test_link_notes_is_idempotent(linked_env: tuple[Path, ZoteroClient, FakeWriter]) -> None:
    vault, client, writer = linked_env
    layout_ = _layout_of(vault)
    link_mod.link_notes(
        client, layout_, vault_name="Vault", link_style="wikilink", dry_run=False, notes_root=vault
    )
    # second run: the child note now exists on the Zotero side
    created = writer.posts[0][3][0]
    http = client.http
    assert isinstance(http, FakeHttp)
    http.routes[f"{BASE}/items?format=json&limit=100&start=0"].append(
        {"key": "NOTE0001", "version": 3, "data": {"key": "NOTE0001", "version": 3, **created}}
    )
    writer.posts.clear()
    report = link_mod.link_notes(
        client, layout_, vault_name="Vault", link_style="wikilink", dry_run=False, notes_root=vault
    )
    assert {r["eid"]: r["status"] for r in report}[EID_A] == "unchanged"
    assert writer.posts == []


def test_link_dry_run_and_file_uri(linked_env: tuple[Path, ZoteroClient, FakeWriter]) -> None:
    vault, client, writer = linked_env
    before = (vault / "Literature" / "A" / "Author-2019-Illustrative.md").read_text(
        encoding="utf-8"
    )
    report = link_mod.link_notes(
        client,
        _layout_of(vault),
        vault_name=None,
        link_style="markdown",
        dry_run=True,
        notes_root=vault,
    )
    assert {r["eid"]: r["status"] for r in report}[EID_A] == "linked"
    assert (vault / "Literature" / "A" / "Author-2019-Illustrative.md").read_text(
        encoding="utf-8"
    ) == before
    assert writer.posts == []


def test_obsidian_uri_encoding() -> None:
    assert (
        link_mod.obsidian_uri("My Vault", "Lit/A/Author-2019.md")
        == "obsidian://open?vault=My%20Vault&file=Lit/A/Author-2019"
    )


# --- fix-metadata --------------------------------------------------------------------------------

MATHML_TITLE = "On <mml:math><mml:mi>L</mml:mi></mml:math>-functions\nof fictional <i>lattices</i>"


def _crossref(title: str, *, doi: str, year: int, venue: str, family: str) -> dict[str, Any]:
    return {
        "message": {
            "title": [title],
            "DOI": doi,
            "issued": {"date-parts": [[year, 3]]},
            "container-title": [venue],
            "author": [{"given": "Example", "family": family}],
        }
    }


def test_fix_metadata_by_doi_and_title_with_jsonl_report(
    layout: DataLayout, tmp_path: Path
) -> None:
    writer = FakeWriter()
    items_route = f"{BASE}/items?format=json&limit=100&start=0"
    query = _crossref(
        "Fictional lattice points near smooth curves",
        doi="10.0000/example.q",
        year=2018,
        venue="J. Fict.",
        family="Author",
    )
    http = FakeHttp(
        {
            items_route: [
                item(
                    "DOI00001",
                    title="old title",
                    DOI="10.0000/example.a",
                    date="2019",
                    publicationTitle="",
                    creators=[],
                    extra=f"eid: {EID_A}",
                ),
                item(
                    "TITL0001",
                    title="Fictional lattice points near smooth curves",
                    date="2017",
                    publicationTitle="",
                    DOI="",
                    creators=[
                        {"creatorType": "author", "firstName": "Example", "lastName": "Author"}
                    ],
                ),
                item(
                    "NOMA0001",
                    title="Completely different work",
                    date="1990",
                    creators=[{"creatorType": "author", "firstName": "Q", "lastName": "Zeta"}],
                ),
                item("DONE0001", title="done", extra="kallimachos: crossref 2026-01-01"),
                {
                    "key": "ATT1",
                    "version": 1,
                    "data": {
                        "key": "ATT1",
                        "version": 1,
                        "itemType": "attachment",
                        "parentItem": "DOI00001",
                        "filename": "x.pdf",
                    },
                },
            ],
            f"{meta_mod.CROSSREF_WORKS}/10.0000/example.a": _crossref(
                MATHML_TITLE,
                doi="10.0000/example.a",
                year=2019,
                venue="Ann. Fict.",
                family="Author",
            ),
            meta_mod.CROSSREF_WORKS: lambda url, params: _query_response(params, query),
        }
    )
    client = ZoteroClient(CFG, http, writer=writer)
    report = tmp_path / "reports" / "zotero-fix.jsonl"
    rows = meta_mod.fix_metadata(
        client,
        http,
        report_path=report,
        cache_path=layout.cache / "zotero_meta.sqlite",
        dry_run=False,
        redo=False,
        today=_dt.date(2026, 10, 18),
    )
    by_key = {r["key"]: r for r in rows}
    assert by_key["DOI00001"]["status"] == "fixed" and by_key["DOI00001"]["source"] == "crossref"
    assert (
        by_key["TITL0001"]["status"] == "fixed"
        and by_key["TITL0001"]["source"] == "crossref-title-author"
    )
    assert by_key["NOMA0001"]["status"] == "unmatched"
    assert by_key["DONE0001"]["status"] == "skipped"
    # strong match only: the written payload keeps key/version/eid and marks extra
    payloads = {p["key"]: p for p in writer.posts[0][3]}
    fixed = payloads["DOI00001"]
    assert fixed["version"] == 1 and fixed["title"] == " ".join(MATHML_TITLE.split())
    assert fixed["publicationTitle"] == "Ann. Fict." and fixed["date"] == "2019-03"
    assert (
        "eid: " + EID_A in fixed["extra"] and "kallimachos: crossref 2026-10-18" in fixed["extra"]
    )
    assert payloads["TITL0001"]["DOI"] == "10.0000/example.q"
    # JSONL report: one object per line even with newline, HTML and MathML in the title
    lines = report.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(rows) == 4
    parsed = [json.loads(ln) for ln in lines]
    assert parsed[0]["new_title"].startswith("On <mml:math>")
    # cache: a second run hits no network for the DOI lookup
    calls_before = len(http.calls)
    meta_mod.fix_metadata(
        client,
        http,
        report_path=report,
        cache_path=layout.cache / "zotero_meta.sqlite",
        dry_run=True,
        redo=True,
        today=_dt.date(2026, 10, 18),
    )
    new_calls = [c for c in http.calls[calls_before:] if "crossref" in c[0]]
    assert new_calls == []


def _query_response(params: dict[str, str], message: dict[str, Any]) -> Any:
    from kallimachos.interfaces import HttpResponse

    if "query.author" in params:
        body = {"message": {"items": [message["message"]]}}
    else:
        body = {"message": {"items": []}}
    return HttpResponse(
        url=meta_mod.CROSSREF_WORKS, status=200, headers={}, body=json.dumps(body).encode("utf-8")
    )


def test_strong_match_rejects_weak_candidates() -> None:
    data = {
        "title": "Fictional lattice points near smooth curves",
        "date": "2017",
        "creators": [{"lastName": "Author"}],
    }
    good = meta_mod.Candidate(
        "crossref",
        "Fictional lattice points near smooth curves",
        year=2018,
        creators=[{"lastName": "Author"}],
    )
    wrong_author = meta_mod.Candidate(
        "crossref",
        "Fictional lattice points near smooth curves",
        year=2018,
        creators=[{"lastName": "Zeta"}],
    )
    far_year = meta_mod.Candidate(
        "crossref",
        "Fictional lattice points near smooth curves",
        year=2012,
        creators=[{"lastName": "Author"}],
    )
    other_title = meta_mod.Candidate(
        "crossref", "Lattice points, a survey", year=2018, creators=[{"lastName": "Author"}]
    )
    assert (
        meta_mod.strong_match(data, [wrong_author, far_year, other_title, good], with_author=True)
        is good
    )
    assert (
        meta_mod.strong_match(data, [wrong_author], with_author=False) is wrong_author
    )  # author ignored
    assert meta_mod.strong_match(data, [far_year], with_author=False) is None


def test_arxiv_parsing() -> None:
    atom = (
        '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">'
        "<entry><title>Fictional\n  arXiv title</title><published>2020-01-02T00:00:00Z</published>"
        "<author><name>Example Author</name></author>"
        "<arxiv:doi>10.0000/example.x</arxiv:doi></entry></feed>"
    )
    cand = meta_mod.arxiv_by_id(lambda _u, _p: atom, "2001.00001")
    assert cand is not None
    assert (
        cand.title == "Fictional arXiv title"
        and cand.year == 2020
        and cand.doi == "10.0000/example.x"
    )
    assert cand.creators == [
        {"creatorType": "author", "firstName": "Example", "lastName": "Author"}
    ]
    assert meta_mod.arxiv_by_id(lambda _u, _p: None, "x") is None
    assert meta_mod.arxiv_id_of({"url": "https://arxiv.org/abs/2001.00001v2"}, []) == "2001.00001"
    assert meta_mod.arxiv_id_of({}, ["Author_2020_Title_arXiv-2001.00002v1.pdf"]) == "2001.00002"


def test_zotero_config_type_accepts_group() -> None:
    client = ZoteroClient(
        ZoteroConfig(enabled=True, user_id="77", library_type="group"),
        FakeHttp(),
        writer=FakeWriter(),
    )
    assert client.base == "https://api.zotero.org/groups/77"
    assert (
        link_mod.zotero_select_uri("K", library_type="group", library_id="77")
        == "zotero://select/groups/77/items/K"
    )
