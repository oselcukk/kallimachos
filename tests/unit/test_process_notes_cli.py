"""``kallimachos process ...`` handlers through ``cli.main`` with the config loader replaced
by a fake (exit codes 0/2/3/5, ``--json``, ``--dry-run``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kallimachos import cli_process_notes
from kallimachos.cli import main
from kallimachos.config.schema import Config
from kallimachos.interfaces import ExitCode, WorkState
from kallimachos.paths import DataLayout
from tests.unit.test_process_notes_support import (
    EID_A,
    argv_log,
    body_md,
    card,
    catalog_entry,
    chain_to,
    fake_lanes,
    make_config,
    make_layout,
    make_vault,
    set_state,
    write_card,
    write_catalog,
    write_semantic,
)

pytestmark = pytest.mark.usefixtures("fake_lanes")
_ = fake_lanes


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, DataLayout, Config]:
    vault = make_vault(tmp_path)
    layout = make_layout(tmp_path)
    config = make_config(vault)

    def fake_load_config(
        path: Path | None = None, *, data_dir: str | None = None
    ) -> tuple[Config, DataLayout]:
        return config, layout

    monkeypatch.setattr(cli_process_notes.config_load, "load_config", fake_load_config)
    write_catalog(layout, catalog_entry())
    write_card(layout, card())
    set_state(layout, EID_A, *chain_to(WorkState.SEMANTIC_VERIFIED))
    return vault, layout, config


def test_note_write_exit_codes(
    env: tuple[Path, DataLayout, Config], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault, layout, _config = env
    body = tmp_path / "note.md"
    body.write_text(body_md(), encoding="utf-8")
    assert main(["process", "note-write", EID_A, "--file", str(body)]) == ExitCode.APPROVAL_REQUIRED
    assert "semantic" in capsys.readouterr().err
    write_semantic(layout, EID_A)
    assert main(["--dry-run", "process", "note-write", EID_A, "--file", str(body)]) == ExitCode.OK
    assert "would write" in capsys.readouterr().out
    assert main(["--json", "process", "note-write", EID_A, "--file", str(body)]) == ExitCode.OK
    out = json.loads(capsys.readouterr().out)
    assert (
        out["created"] is True and out["sync"] == "succeeded" and out["frontmatter"]["eid"] == EID_A
    )
    assert Path(out["path"]).is_file()
    # second run: name clash -> 5 with the candidate path
    assert main(["process", "note-write", EID_A, "--file", str(body)]) == ExitCode.APPROVAL_REQUIRED
    assert out["path"] in capsys.readouterr().err
    # bad body -> 2
    body.write_text("---\nx: 1\n---\n# T\n", encoding="utf-8")
    assert main(["process", "note-write", EID_A, "--file", str(body)]) == ExitCode.USAGE
    assert (
        main(["process", "note-write", EID_A, "--file", str(tmp_path / "missing.md")])
        == ExitCode.USAGE
    )
    # sync conflict -> 3, note still written
    (vault / ".fake-sync-conflict").write_text("", encoding="utf-8")
    body.write_text(body_md(), encoding="utf-8")
    existing = Path(out["path"])
    assert (
        main(["process", "note-write", EID_A, "--file", str(body), "--existing", str(existing)])
        == ExitCode.PARTIAL
    )
    assert "conflict" in capsys.readouterr().err
    assert "kallimachos:begin full_reading" in existing.read_text(encoding="utf-8")


def test_links_and_fix_fake_cli(
    env: tuple[Path, DataLayout, Config], capsys: pytest.CaptureFixture[str]
) -> None:
    vault, layout, _config = env
    set_state(layout, EID_A, *chain_to(WorkState.NOTE_WRITTEN))
    assert main(["--json", "process", "links"]) == ExitCode.OK
    out = json.loads(capsys.readouterr().out)
    assert out["apply"] is False
    assert main(["process", "links", "--apply"]) == ExitCode.OK
    bad = vault / "Literature/Example-Channel/01-Topic/Bad-2021-Note.md"
    bad.write_text("# Bad\n\n[[a,0],[0,b]]\n", encoding="utf-8")
    assert main(["process", "links", "fix-fake"]) == ExitCode.OK
    assert "would fix 1 link" in capsys.readouterr().out
    assert "[[a,0],[0,b]]" in bad.read_text(encoding="utf-8")
    assert main(["process", "links", "fix-fake", "--all"]) == ExitCode.APPROVAL_REQUIRED
    capsys.readouterr()
    assert main(["--yes", "process", "links", "fix-fake", "--all", "--apply"]) == ExitCode.OK
    assert "[ [a,0],[0,b] ]" in bad.read_text(encoding="utf-8")


def test_cluster_cli(
    env: tuple[Path, DataLayout, Config], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault, layout, _config = env
    folder = "Example-Channel/01-Topic"
    (vault / "Literature" / folder / "A-2021-First.md").write_text(
        f"---\neid: {EID_A}\n---\n# First\n", encoding="utf-8"
    )
    set_state(layout, EID_A, *chain_to(WorkState.NOTE_WRITTEN))
    assert main(["process", "cluster", "start", folder]) == ExitCode.APPROVAL_REQUIRED
    capsys.readouterr()
    set_state(layout, EID_A, WorkState.LINKED)
    set_state(layout, "0123456789ab", *chain_to(WorkState.LINKED))
    assert main(["--json", "process", "cluster", "start", folder]) == ExitCode.OK
    packet = json.loads(capsys.readouterr().out)
    assert "## 1. Map of the cluster" in packet["skeleton_md"] and len(packet["eids"]) == 2
    summary = tmp_path / "summary.md"
    summary.write_text(
        "# Cluster summary\n\nFrom notes and graph only; unsourced text carries [comment].\n\n"
        "## 1. Map of the cluster\n\n"
        "- [[Literature/Example-Channel/01-Topic/A-2021-First]] starts it.\n\n"
        "## 2. Notation glossary\n\nNone.\n\n## 3. Numerical results\n\nNone.\n\n"
        "## 4. Errors and corrections\n\nNone.\n\n## 5. Suggested concept notes\n\nNone.\n",
        encoding="utf-8",
    )
    assert (
        main(["--dry-run", "process", "cluster", "write", folder, "--file", str(summary)])
        == ExitCode.OK
    )
    assert not (vault / "Literature" / folder / "_Cluster-Summary.md").exists()
    assert (
        main(["process", "cluster", "write", folder, "--file", str(summary), "--verified-by", "v"])
        == ExitCode.OK
    )
    assert (vault / "Literature" / folder / "_Cluster-Summary.md").is_file()
    capsys.readouterr()
    assert main(["process", "cluster", "open-problems", folder]) == ExitCode.OK
    assert "| # | Work |" in capsys.readouterr().out


def test_queue_and_index_cli(
    env: tuple[Path, DataLayout, Config], capsys: pytest.CaptureFixture[str]
) -> None:
    vault, _layout, _config = env
    assert main(["process", "queue", "add", EID_A, "--kind", "read", "--note", "n"]) == ExitCode.OK
    assert "queued read: On the Distribution" in capsys.readouterr().out
    assert main(["process", "queue", "add", "arXiv:9999.00000", "--kind", "ask"]) == ExitCode.USAGE
    capsys.readouterr()
    assert (
        main(["process", "queue", "add", "arXiv:9999.00000", "--kind", "ask", "--title", "T"])
        == ExitCode.OK
    )
    capsys.readouterr()
    assert main(["--json", "--dry-run", "process", "queue", "sync"]) == ExitCode.OK
    assert len(json.loads(capsys.readouterr().out)["pending"]) == 2
    assert argv_log(vault) == []
    assert main(["--json", "process", "queue", "sync"]) == ExitCode.OK
    synced = json.loads(capsys.readouterr().out)["synced"]
    assert {e["synced"]["avenox_task_id"] for e in synced} == {
        f"kallimachos-read-{EID_A}",
        "kallimachos-read-arxiv-9999-00000",
    }
    assert main(["process", "notes-index", "rebuild"]) == ExitCode.OK
    assert "indexed 1 note" in capsys.readouterr().out
    assert main(["process", "notes-index", "find", "Other", "2020"]) == ExitCode.OK
    assert "0123456789ab" in capsys.readouterr().out
    assert main(["process", "notes-index", "find", "Nobody"]) == ExitCode.OK
    assert "no note found" in capsys.readouterr().out


def test_load_profile_reads_yaml(env: tuple[Path, DataLayout, Config]) -> None:
    _vault, layout, _config = env
    assert cli_process_notes.load_profile(layout).channels == []
    layout.profile_file.write_text(
        "schema_version: 1\nstatus: approved\nfield: f\nchannels:\n"
        "  - id: EX\n    name: Example\n    folder: Example-Channel\n",
        encoding="utf-8",
    )
    profile = cli_process_notes.load_profile(layout)
    assert profile.is_approved and profile.channels[0].folder == "Example-Channel"
    layout.profile_file.write_text("- not a mapping\n", encoding="utf-8")
    with pytest.raises(Exception, match="mapping"):
        cli_process_notes.load_profile(layout)
