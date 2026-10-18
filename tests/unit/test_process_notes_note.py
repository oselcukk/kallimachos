"""``process note-write``: naming, path, frontmatter,
body validation, overwrite refusal, ``--existing`` idempotency, Avenox sync conflict."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from kallimachos.config.schema import Config
from kallimachos.interfaces import ApprovalRequired, NoteFrontmatter, UsageError, WorkState, dump
from kallimachos.paths import DataLayout
from kallimachos.process import note
from kallimachos.process.notes_index import NotesIndex, parse_frontmatter
from kallimachos.state import store
from tests.unit.test_process_notes_support import (
    EID_A,
    EID_B,
    argv_log,
    body_md,
    card,
    catalog_entry,
    chain_to,
    fake_lanes,
    make_config,
    make_layout,
    make_profile,
    make_vault,
    set_state,
    write_card,
    write_catalog,
    write_semantic,
)

pytestmark = pytest.mark.usefixtures("fake_lanes")
_ = fake_lanes


# ---------------------------------------------------------------------------
# Name and path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Example Author", "Author"),
        ("Author, Example", "Author"),
        ("EXAMPLE SURNAME", "Surname"),
        ("Ali Örnek", "Ornek"),
        ("Jean van der Example", "Van-Der-Example"),
        ("Pat McDonald", "McDonald"),
    ],
)
def test_surname_of(name: str, expected: str) -> None:
    assert note.surname_of(name) == expected


def test_short_title_rules() -> None:
    assert note.short_title("On the $\\zeta$-function and \\emph{Fictional} Sums") == (
        "Zeta-Function-And-Fictional-Sums"
    )
    long = "A very long fictional title with far too many words to keep in a file name"
    short = note.short_title(long)
    assert len(short) <= 60 and len(short.split("-")) <= 7
    assert note.short_title("") == "Untitled"


def test_note_filename_etal_and_suffix() -> None:
    three = ["Example Author A", "Example Author B", "Example Author C"]
    assert note.note_filename(three, 2021, "Fictional Sums", etal_suffix="etal") == (
        "A-B-C-2021-Fictional-Sums.md"
    )
    four = [*three, "Example Author D"]
    assert note.note_filename(four, 2021, "Fictional Sums", etal_suffix="etal") == (
        "A-etal-2021-Fictional-Sums.md"
    )
    assert note.note_filename(four, None, "Fictional Sums", etal_suffix="vd") == (
        "A-vd-undated-Fictional-Sums.md"
    )
    assert note.SAFE_NAME_RE.match(note.note_filename(four, 2021, "x", etal_suffix="etal")[:-3])


def test_note_path_mirrors_archive_subfolder(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    config = make_config(vault)
    path = note.note_path(catalog_entry(), card(), config, make_profile())
    assert path == vault / "Literature" / "Example-Channel" / "01-Topic" / (
        "A-B-2021-Distribution-Of-Fictional-Zeta-Sums.md"
    )
    entry = catalog_entry(path="")
    assert note.note_path(entry, card(), config, make_profile()).parent == (
        vault / "Literature" / "Example-Channel"
    )


# ---------------------------------------------------------------------------
# Body validation
# ---------------------------------------------------------------------------


def test_validate_body_ok_and_warnings() -> None:
    assert note.validate_body(body_md(), lang="en", relation_section=True) == []
    warns = note.validate_body(body_md(relation=None), lang="en", relation_section=True)
    assert any("Relation to my work" in w for w in warns)
    warns = note.validate_body(
        body_md(relation="This sentence lacks the tag."), lang="en", relation_section=True
    )
    assert any("[inference]" in w for w in warns)


def test_validate_body_hard_failures() -> None:
    with pytest.raises(UsageError, match="frontmatter"):
        note.validate_body("---\nkind: note\n---\n# T\n", lang="en")
    with pytest.raises(UsageError, match="H1"):
        note.validate_body("## 1. Citation\n\n## 11. Not found\nnone\n", lang="en")
    with pytest.raises(UsageError, match="Not found"):
        note.validate_body("# T\n\n## 1. Citation\n\nx\n", lang="en")
    with pytest.raises(UsageError, match="fake links"):
        note.validate_body(body_md(extra="\nMatrix [[a,0],[0,b]] here.\n"), lang="en")


def test_validate_body_missing_section_is_a_warning_and_tr_headings() -> None:
    body = body_md().replace("## 6. Method\n\nElementary estimates.\n\n", "")
    warns = note.validate_body(body, lang="en")
    assert warns == ["missing section 'Method'"]
    tr_body = "# T\n\n## 1. Künye\n\nx\n\n## 11. Bulunamadı\n\nyok\n"
    warns = note.validate_body(tr_body, lang="tr")
    assert "missing section 'Künye'" not in warns
    assert "missing section 'Özet'" in warns


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------


def test_build_and_render_frontmatter_is_exactly_note_1(tmp_path: Path) -> None:
    config = make_config(make_vault(tmp_path))
    config.notes.frontmatter.extra = {"tags": ["literature"], "eid": "ignored"}
    fm = note.build_frontmatter(catalog_entry(), card(), config, make_profile(), reader="r")
    assert set(dump(fm)) == set(NoteFrontmatter.__dataclass_fields__)
    text = note.render_frontmatter(fm)
    parsed, body = parse_frontmatter(text)
    assert body == ""
    assert parsed["kallimachos"] == "note/1"
    assert parsed["eid"] == EID_A and parsed["channel"] == "EX" and parsed["reader"] == "r"
    assert parsed["project"] == "example-project" and parsed["kind"] == "note"
    assert parsed["visibility"] == "internal"
    assert isinstance(parsed["updated_at"], _dt.date)
    assert parsed["tags"] == ["literature"]
    assert "zotero" not in parsed  # None fields are not written
    assert text.count("eid:") == 1  # extra never overrides a note/1 field


def test_update_frontmatter_fields_only_touches_given_keys() -> None:
    text = "---\nkind: note\ntags:\n  - a\n  - b\nproject: p\n---\n# Title\n\nBody\n"
    out = note.update_frontmatter_fields(text, {"eid": EID_A, "project": "q"})
    fm, body = parse_frontmatter(out)
    assert fm == {"kind": "note", "tags": ["a", "b"], "project": "q", "eid": EID_A}
    assert body == "# Title\n\nBody\n"
    again = note.update_frontmatter_fields(out, {"eid": EID_A, "project": "q"})
    assert again == out
    no_fm = note.update_frontmatter_fields("# Title\n", {"eid": EID_A})
    assert no_fm.startswith(f"---\neid: {EID_A}\n---\n# Title")


def test_upsert_managed_block_idempotent_and_in_place() -> None:
    text = (
        "intro\n\n<!-- kallimachos:begin full_reading eid=x -->\nold\n"
        "<!-- kallimachos:end full_reading -->\n\ntail\n"
    )
    out = note.upsert_managed_block(text, kind="full_reading", block="new", attrs={"eid": "y"})
    assert out == (
        "intro\n\n<!-- kallimachos:begin full_reading eid=y -->\nnew\n"
        "<!-- kallimachos:end full_reading -->\n\ntail\n"
    )
    assert (
        note.upsert_managed_block(out, kind="full_reading", block="new", attrs={"eid": "y"}) == out
    )
    appended = note.upsert_managed_block("body\n", kind="related_notes", block="- x", attrs={})
    assert appended.endswith(
        "body\n\n<!-- kallimachos:begin related_notes -->\n- x\n"
        "<!-- kallimachos:end related_notes -->\n"
    )


# ---------------------------------------------------------------------------
# write_note pipeline
# ---------------------------------------------------------------------------


def _setup(tmp_path: Path, *, avenox: bool = True) -> tuple[Path, DataLayout, Config]:
    vault = make_vault(tmp_path, avenox=avenox)
    layout = make_layout(tmp_path)
    write_catalog(layout, catalog_entry())
    write_card(layout, card())
    set_state(layout, EID_A, *chain_to(WorkState.SEMANTIC_VERIFIED))
    return vault, layout, make_config(vault, avenox=avenox)


def test_write_note_requires_semantic_pass(tmp_path: Path) -> None:
    _vault, layout, config = _setup(tmp_path)
    with pytest.raises(ApprovalRequired, match="missing"):
        note.write_note(layout, config, make_profile(), EID_A, body_md())
    write_semantic(layout, EID_A, "fail")
    with pytest.raises(ApprovalRequired, match="fail"):
        note.write_note(layout, config, make_profile(), EID_A, body_md())


def test_write_note_creates_note_index_sync_and_state(tmp_path: Path) -> None:
    vault, layout, config = _setup(tmp_path)
    write_semantic(layout, EID_A)
    result = note.write_note(layout, config, make_profile(), EID_A, body_md())
    assert result.created and result.sync_result == "succeeded" and result.warnings == []
    text = result.path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    assert fm["eid"] == EID_A and fm["kallimachos"] == "note/1" and fm["reader"] == "example-reader"
    assert fm["source_pdf"] == "Example-Channel/01-Topic/Author_2021_Fictional.pdf"
    assert body.lstrip("\n").startswith("# Example Author A")
    index = NotesIndex.load(layout.notes_index_file)
    ref = index.get(EID_A)
    assert ref is not None and ref.path == (
        "Literature/Example-Channel/01-Topic/A-B-2021-Distribution-Of-Fictional-Zeta-Sums.md"
    )
    assert [a[-1] for a in argv_log(vault)] == ["sync"]
    states = list(store.read_jsonl(layout.status_file))
    assert states[-1]["eid"] == EID_A and states[-1]["state"] == WorkState.NOTE_WRITTEN.value
    assert states[-1]["by"] == "process note-write"


def test_write_note_refuses_to_overwrite_with_candidate_path(tmp_path: Path) -> None:
    _vault, layout, config = _setup(tmp_path)
    write_semantic(layout, EID_A)
    first = note.write_note(layout, config, make_profile(), EID_A, body_md())
    with pytest.raises(ApprovalRequired) as exc:
        note.write_note(layout, config, make_profile(), EID_A, body_md())
    assert str(first.path) in str(exc.value) and "--existing" in str(exc.value)


def test_write_note_existing_touches_only_managed_block_and_allowed_keys(tmp_path: Path) -> None:
    vault, layout, config = _setup(tmp_path)
    write_semantic(layout, EID_A)
    existing = vault / "Literature/Example-Channel/01-Topic/Author-2019-Example-Title.md"
    original = existing.read_text(encoding="utf-8")
    result = note.write_note(layout, config, make_profile(), EID_A, body_md(), existing=existing)
    assert not result.created and result.path == existing
    text = existing.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    # user frontmatter kept, only the allowed keys added
    assert fm["tags"] == ["legacy"] and fm["project"] == "example-project"
    assert set(fm) - {"kind", "project", "visibility", "tags"} <= set(
        note.EXISTING_FRONTMATTER_KEYS
    )
    assert fm["eid"] == EID_A and "kind" in fm
    # user body untouched, block appended with demoted headings
    user_body = parse_frontmatter(original)[1]
    assert body.startswith(user_body.rstrip("\n"))
    assert "<!-- kallimachos:begin full_reading eid=" in text
    assert "## Full reading (example-reader, " in text
    assert "### 1. Citation" in text and "\n## 1. Citation" not in text
    # idempotent: a second run with the same body changes nothing
    note.write_note(layout, config, make_profile(), EID_A, body_md(), existing=existing)
    assert existing.read_text(encoding="utf-8") == text
    # a new reading replaces the previous block in place
    note.write_note(
        layout,
        config,
        make_profile(),
        EID_A,
        body_md().replace("Five sections.", "Six sections."),
        existing=existing,
        reader="other-model",
    )
    text2 = existing.read_text(encoding="utf-8")
    assert text2.count("kallimachos:begin full_reading") == 1
    assert "Six sections." in text2 and "Five sections." not in text2
    assert "example-reader" not in text2.split("---", 2)[2]  # previous model's block removed
    assert parse_frontmatter(text2)[0]["reader"] == "other-model"


def test_write_note_sync_conflict_is_reported_not_fatal(tmp_path: Path) -> None:
    vault, layout, config = _setup(tmp_path)
    write_semantic(layout, EID_A)
    (vault / ".fake-sync-conflict").write_text("", encoding="utf-8")
    result = note.write_note(layout, config, make_profile(), EID_A, body_md())
    assert result.sync_result == "conflict" and result.path.is_file()
    assert any("conflict" in w for w in result.warnings)


def test_write_note_plain_folder_skips_sync(tmp_path: Path) -> None:
    vault, layout, config = _setup(tmp_path, avenox=False)
    write_semantic(layout, EID_A)
    result = note.write_note(layout, config, make_profile(), EID_A, body_md())
    assert result.sync_result is None and result.path.is_file()
    assert argv_log(vault) == []


def test_write_note_dry_run_writes_nothing(tmp_path: Path) -> None:
    vault, layout, config = _setup(tmp_path)
    write_semantic(layout, EID_A)
    result = note.write_note(layout, config, make_profile(), EID_A, body_md(), dry_run=True)
    assert not result.path.exists() and argv_log(vault) == []
    assert not layout.notes_index_file.exists()


def test_existing_note_can_be_found_by_eid_fixture() -> None:
    assert EID_B == "0123456789ab"


# ---------------------------------------------------------------------------
# --existing is confined to literature notes (adversarial audit, finding 3)
# ---------------------------------------------------------------------------


def test_existing_note_outside_the_literature_folder_is_refused(tmp_path: Path) -> None:
    vault, layout, config = _setup(tmp_path)
    write_semantic(layout, EID_A)
    outside = tmp_path / "home" / ".zshrc"
    outside.parent.mkdir(parents=True)
    outside.write_text("export X=1\n", encoding="utf-8")
    for target in (
        outside,
        Path("../home/.zshrc"),  # relative to the vault root, escaping it
        vault / "Literature" / ".." / ".." / "home" / ".zshrc",
        vault / "Daily" / "2026-10-18.md",  # inside the vault but not a literature note
        vault / "Literature" / "Example-Channel",  # a folder, not a .md
    ):
        with pytest.raises(UsageError):
            note.write_note(layout, config, make_profile(), EID_A, body_md(), existing=target)
    assert outside.read_text(encoding="utf-8") == "export X=1\n"


def test_existing_note_through_a_symlink_component_is_refused(tmp_path: Path) -> None:
    vault, layout, config = _setup(tmp_path)
    write_semantic(layout, EID_A)
    real_dir = tmp_path / "elsewhere"
    real_dir.mkdir()
    (real_dir / "target.md").write_text("# mine\n", encoding="utf-8")
    link = vault / "Literature" / "linked"
    link.symlink_to(real_dir, target_is_directory=True)
    with pytest.raises(UsageError):
        note.write_note(
            layout, config, make_profile(), EID_A, body_md(), existing=link / "target.md"
        )
    assert (real_dir / "target.md").read_text(encoding="utf-8") == "# mine\n"


def test_existing_note_relative_to_the_vault_root_is_accepted(tmp_path: Path) -> None:
    vault, layout, config = _setup(tmp_path)
    write_semantic(layout, EID_A)
    rel = Path("Literature/Example-Channel/01-Topic/Author-2019-Example-Title.md")
    result = note.write_note(layout, config, make_profile(), EID_A, body_md(), existing=rel)
    assert result.path == vault / rel and not result.created


def test_note_subfolder_drops_parent_components() -> None:
    entry = catalog_entry(EID_A)
    entry.path = "../../outside/01-Topic/file.pdf"
    assert ".." not in note.note_subfolder(entry, make_profile()).parts
    entry.path = "Example-Channel/../../x/file.pdf"
    assert ".." not in note.note_subfolder(entry, make_profile()).parts
    entry.path = "Example-Channel/01-Topic/file.pdf"
    assert note.note_subfolder(entry, make_profile()) == Path("Example-Channel/01-Topic")


def test_body_with_kallimachos_markers_is_rejected() -> None:
    body = body_md().replace("# ", "<!-- kallimachos:end full_reading -->\n# ", 1)
    with pytest.raises(UsageError):
        note.validate_body(body, lang="en")
