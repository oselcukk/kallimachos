"""``kallimachos process ...`` core commands end to end on a synthetic work: extract -> quote ->
card-write -> check (--pre writes nothing) -> semantic-record -> status -> cite -> migrate-card."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kallimachos import cli_process
from kallimachos.cli import main
from kallimachos.config.schema import Config
from kallimachos.interfaces import WorkState, dump
from kallimachos.paths import DataLayout
from kallimachos.process import extract as extract_mod
from kallimachos.process import status as status_mod
from tests.unit.conftest_pdf import (  # noqa: F401
    EID_A,
    PAGE_1,
    PAGE_2,
    QUOTE_OK,
    FakeCatalog,
    config_fixture,
    fake_config_fixture,
    fake_store,
    layout_fixture,
    make_card,
    make_entry,
    make_pdf,
)


@pytest.fixture
def work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_config: Config, layout: DataLayout
) -> tuple[str, Path]:
    pdf = make_pdf(tmp_path / "Author_2019_Illustrative.pdf", [PAGE_1, PAGE_2])
    eid = extract_mod.eid_for(pdf)
    monkeypatch.setattr(extract_mod, "pdf_to_pages", lambda *_a, **_k: [PAGE_1, PAGE_2])
    monkeypatch.setattr(
        cli_process, "_catalog", lambda _layout: FakeCatalog([make_entry(eid, path=pdf.name)])
    )
    fake_config.archive.root = str(tmp_path)
    return eid, pdf


def _card_file(tmp_path: Path, eid: str, **kw: object) -> str:
    path = tmp_path / "card.json"
    path.write_text(json.dumps(dump(make_card(eid, **kw))), encoding="utf-8")
    return str(path)


def test_full_core_pipeline(
    work: tuple[str, Path], layout: DataLayout, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    eid, pdf = work
    assert main(["process", "extract", str(pdf)]) == 0
    assert status_mod.current_state(layout, eid) is WorkState.TEXT_EXTRACTED
    assert extract_mod.load_pages(layout, eid) == [PAGE_1, PAGE_2]
    # extracting again is skipped (idempotent) and by eid through the catalog
    assert main(["process", "extract", eid]) == 0
    assert "skipped" in capsys.readouterr().out

    assert main(["process", "quote", eid, "1", "For every fictional", "--end", "lattice."]) == 0
    assert capsys.readouterr().out.strip() == QUOTE_OK
    assert main(["process", "quote", eid, "1", "nowhere to be found"]) == 1
    assert main(["process", "quote", eid, "7", "For every"]) == 2
    assert main(["process", "quote", eid, "1", "For every", "--end", "x", "--length", "3"]) == 2

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**dump(make_card(eid)), "dead_key": 1}), encoding="utf-8")
    assert main(["process", "card-write", eid, "--file", str(bad)]) == 2
    assert main(["process", "card-write", eid, "--file", _card_file(tmp_path, eid)]) == 0
    assert status_mod.current_state(layout, eid) is WorkState.CARD_WRITTEN

    assert main(["process", "check", eid, "--pre"]) == 0
    assert status_mod.current_state(layout, eid) is WorkState.CARD_WRITTEN  # --pre wrote nothing
    assert not (layout.checks_dir / f"{eid}.json").exists()
    capsys.readouterr()
    assert main(["--json", "process", "check", eid]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["result"] == "check_passed"
    assert (layout.checks_dir / f"{eid}.json").is_file()
    assert status_mod.current_state(layout, eid) is WorkState.CHECK_PASSED

    result = tmp_path / "result.json"
    result.write_text(
        json.dumps(
            {
                "schema": "kallimachos.semantic_result/1",
                "verifier_model": "example-verifier",
                "at": "2026-10-18T12:00:00+00:00",
                "result": "pass",
                "mismatches": [],
            }
        ),
        encoding="utf-8",
    )
    assert main(["process", "semantic-record", eid, "--file", str(result)]) == 0
    assert status_mod.current_state(layout, eid) is WorkState.SEMANTIC_VERIFIED
    assert (layout.semantic_dir / f"{eid}.json").is_file()

    capsys.readouterr()
    assert main(["--json", "process", "status"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == [
        {
            "eid": eid,
            "state": "semantic_verified",
            "at": rows[0]["at"],
            "by": "process semantic-record",
        }
    ]
    assert main(["process", "status", "--summary"]) == 0
    assert "semantic_verified: 1" in capsys.readouterr().out

    assert main(["process", "cite"]) == 0
    assert layout.graph_file.is_file() and layout.graph_uncertain_file.is_file()
    assert layout.graph_file.read_text(encoding="utf-8") == ""  # the only reference is not archived


def test_check_failed_state_and_redelivery(
    work: tuple[str, Path], layout: DataLayout, tmp_path: Path
) -> None:
    eid, pdf = work
    from kallimachos.interfaces import CardItem, ItemType

    assert main(["process", "extract", str(pdf)]) == 0
    broken = [CardItem(ItemType.THEOREM, "Theorem 1.1", 1, "q" * 50, "s", number="1.1")]
    assert (
        main(["process", "card-write", eid, "--file", _card_file(tmp_path, eid, items=broken)]) == 0
    )
    assert main(["process", "check", eid]) == 0
    assert status_mod.current_state(layout, eid) is WorkState.CHECK_FAILED
    assert main(["process", "card-write", eid, "--file", _card_file(tmp_path, eid)]) == 0
    assert status_mod.current_state(layout, eid) is WorkState.CARD_WRITTEN
    assert main(["process", "check", eid]) == 0
    assert status_mod.current_state(layout, eid) is WorkState.CHECK_PASSED


def test_card_write_wrong_eid_and_missing_text(work: tuple[str, Path], tmp_path: Path) -> None:
    eid, _pdf = work
    assert main(["process", "card-write", "ffffffffffff", "--file", _card_file(tmp_path, eid)]) == 2
    # no text yet: card_written is not an entry state
    assert main(["process", "card-write", eid, "--file", _card_file(tmp_path, eid)]) == 2


def test_dry_run_writes_nothing(work: tuple[str, Path], layout: DataLayout, tmp_path: Path) -> None:
    eid, pdf = work
    assert main(["--dry-run", "process", "extract", str(pdf)]) == 0
    assert not layout.status_file.exists()
    assert main(["process", "extract", str(pdf)]) == 0
    assert (
        main(["--dry-run", "process", "card-write", eid, "--file", _card_file(tmp_path, eid)]) == 0
    )
    assert status_mod.current_state(layout, eid) is WorkState.TEXT_EXTRACTED


def test_migrate_card_cli(
    tmp_path: Path, fake_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    old = tmp_path / "old.json"
    old.write_text(
        json.dumps({"eid": EID_A, "ozet_tr": "x", "ogeler": [{"onem": "ana"}]}), encoding="utf-8"
    )
    assert main(["process", "migrate-card", "--from", "tr-v4", "--file", str(old)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["summary"] == "x" and out["items"][0]["importance"] == "main"
    target = tmp_path / "new.json"
    assert (
        main(
            ["process", "migrate-card", "--from", "tr-v4", "--file", str(old), "--out", str(target)]
        )
        == 0
    )
    assert json.loads(target.read_text(encoding="utf-8"))["schema"] == "kallimachos.card/4"


def test_chunk_create_cli(
    fake_config: Config,
    layout: DataLayout,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli_process, "_catalog", lambda _layout: FakeCatalog([make_entry(EID_A)]))
    assert main(["--json", "process", "chunk", "create", "--channel", "A", "--size", "5"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["chunk"] == 1 and payload["works"] == 1
    assert (layout.chunks_dir / "1" / "log.md").is_file()


@pytest.mark.parametrize(
    "argv",
    [
        ["process", "check", "../../../evil"],
        ["process", "quote", "ZZZZZZZZZZZZ", "1", "x"],
        ["process", "card-write", "a1b2c3d4e5f", "--file", "x.json"],
        ["process", "semantic-record", "A1B2C3D4E5F6", "--file", "x.json"],
        ["process", "note-write", "../x", "--file", "x.md"],
    ],
)
def test_positional_eid_is_validated_by_argparse(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert "not an eid" in capsys.readouterr().err
