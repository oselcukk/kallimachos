"""Profile acceptance through the CLI: init (Q&A), profile build (files, ORCID, OpenAlex),
profile show / approve, approval gates (exit 5). No network: fixtures only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from kallimachos.cli import main
from kallimachos.interfaces import ExitCode, build
from kallimachos.paths import DataLayout
from kallimachos.profile.offline import ENV_FIXTURES
from kallimachos.profile.schema import Profile

ROOT = Path(__file__).resolve().parents[2]
ANSWERS = ROOT / "tests" / "e2e" / "answers_qa.yaml"
LIBRARY = ROOT / "tests" / "e2e" / "sample_library"
FIXTURES = ROOT / "tests" / "fixtures" / "profile" / "http"
EXAMPLE = ROOT / "profiles" / "example-number-theory.yaml"
ORCID_DEMO = "0000-0002-1825-0097"


@pytest.fixture
def home(_isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(ENV_FIXTURES, str(FIXTURES))
    return _isolated_home


def _profile(home: Path) -> Profile:
    return build(Profile, yaml.safe_load((home / "profile.yaml").read_text("utf-8")))


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_init_qa_path_writes_draft_then_requires_reconfigure(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init", "--answers", str(ANSWERS)]) == ExitCode.OK
    out = capsys.readouterr().out
    assert "profile approve" in out
    assert (home / "config.yaml").exists() and (home / "profile.yaml").exists()
    assert (home / "state" / "locks").is_dir()
    profile = _profile(home)
    assert profile.status == "draft" and not profile.is_approved
    config = yaml.safe_load((home / "config.yaml").read_text("utf-8"))
    assert config["language"] == "en" and config["sources"]["zbmath"]["enabled"] is False
    assert "sk-" not in (home / "config.yaml").read_text("utf-8")

    # second run without --reconfigure: approval required
    assert main(["init", "--answers", str(ANSWERS)]) == ExitCode.APPROVAL_REQUIRED
    assert "--reconfigure" in capsys.readouterr().err
    # reconfigure without --yes: differences shown, still exit 5
    assert (
        main(
            [
                "--data-dir",
                str(home),
                "init",
                "--answers",
                str(ANSWERS),
                "--lang",
                "tr",
                "--reconfigure",
            ]
        )
        == ExitCode.APPROVAL_REQUIRED
    )
    err = capsys.readouterr().err
    assert "language: 'en' -> 'tr'" in err and "--yes" in err
    assert yaml.safe_load((home / "config.yaml").read_text("utf-8"))["language"] == "en"
    # approved: rewritten
    assert (
        main(
            ["--yes", "--json", "init", "--answers", str(ANSWERS), "--lang", "tr", "--reconfigure"]
        )
        == ExitCode.OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["profile_status"] == "draft" and payload["channels"] == ["A", "B"]
    assert yaml.safe_load((home / "config.yaml").read_text("utf-8"))["language"] == "tr"


def test_init_dry_run_writes_nothing(home: Path) -> None:
    assert main(["--dry-run", "init", "--answers", str(ANSWERS)]) == ExitCode.OK
    assert not (home / "config.yaml").exists() and not (home / "profile.yaml").exists()


def test_init_invalid_answers_exit_2(home: Path, tmp_path: Path) -> None:
    bad = tmp_path / "a.yaml"
    bad.write_text("language: en\nunknown_key: 1\n", encoding="utf-8")
    assert main(["init", "--answers", str(bad)]) == ExitCode.USAGE
    bad.write_text(
        "language: en\nkeys: {openalex: 'ghp_literal-not-a-reference'}\n", encoding="utf-8"
    )
    assert main(["init", "--answers", str(bad)]) == ExitCode.USAGE


# ---------------------------------------------------------------------------
# show / approve
# ---------------------------------------------------------------------------


def test_show_and_approve(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["profile", "show"]) == ExitCode.USAGE  # no profile yet
    assert main(["init", "--answers", str(ANSWERS)]) == ExitCode.OK
    capsys.readouterr()
    assert main(["profile", "show", "--table"]) == ExitCode.OK
    out = capsys.readouterr().out
    assert "| A | Analytic number theory |" in out and "status=draft" in out
    assert "Example Advisor" in out and "riemann zeta function" in out
    assert main(["--json", "profile", "show"]) == ExitCode.OK
    assert json.loads(capsys.readouterr().out)["status"] == "draft"

    assert main(["--dry-run", "profile", "approve"]) == ExitCode.OK
    assert _profile(home).status == "draft"
    assert main(["profile", "approve"]) == ExitCode.OK
    approved = _profile(home)
    assert approved.is_approved and approved.approved_at is not None
    assert approved.approved_at.tzinfo is not None
    assert "approved" in capsys.readouterr().out


def test_show_example_profile_validates(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["profile", "show", str(EXAMPLE)]) == ExitCode.OK
    assert "Analytic number theory" in capsys.readouterr().out


def test_show_invalid_profile_exit_2(home: Path, tmp_path: Path) -> None:
    bad = tmp_path / "p.yaml"
    bad.write_text("schema_version: 1\nstatus: maybe\n", encoding="utf-8")
    assert main(["profile", "show", str(bad)]) == ExitCode.USAGE
    bad.write_text("schema_version: 1\nchannelz: []\n", encoding="utf-8")
    assert main(["profile", "show", str(bad)]) == ExitCode.USAGE


def test_approve_without_channels_exit_2(home: Path) -> None:
    (home / "profile.yaml").write_text("schema_version: 1\nstatus: draft\n", encoding="utf-8")
    assert main(["profile", "approve"]) == ExitCode.USAGE


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def test_build_requires_a_source(home: Path) -> None:
    assert main(["profile", "build"]) == ExitCode.USAGE
    assert main(["profile", "build", "--from-files", str(home / "nope")]) == ExitCode.USAGE


def test_build_from_files_acceptance(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["profile", "build", "--from-files", str(LIBRARY)]) == ExitCode.OK
    out = capsys.readouterr().out
    assert "# Profile draft from files" in out and "Skipped paths" in out
    assert "10.0000/private" not in out
    profile = _profile(home)
    assert profile.status == "draft"
    assert len(profile.seeds) >= 2
    assert len(profile.channels) >= 1
    assert sum(len(c.journals) for c in profile.channels) >= 1
    report = (home / "profile-report.md").read_text("utf-8")
    assert "students-grades.bib" in report  # listed as skipped, never opened
    assert "10.0000/private" not in report


def test_build_merges_qa_and_files_and_guards_approved(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "--json",
                "profile",
                "build",
                "--qa",
                str(ANSWERS),
                "--from-files",
                str(LIBRARY / "references.bib"),
                "--field",
                "fictional-number-theory",
            ]
        )
        == ExitCode.OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "draft" and payload["channels"] >= 3
    profile = _profile(home)
    assert profile.field == "fictional-number-theory"
    assert len({c.id for c in profile.channels}) == len(profile.channels)
    assert any(a.name == "Example Advisor" for a in profile.authors.always_show)
    assert main(["profile", "approve"]) == ExitCode.OK
    capsys.readouterr()
    assert main(["profile", "build", "--qa", str(ANSWERS)]) == ExitCode.APPROVAL_REQUIRED
    assert _profile(home).is_approved
    assert main(["--yes", "profile", "build", "--qa", str(ANSWERS)]) == ExitCode.OK
    assert not _profile(home).is_approved


def test_build_from_orcid_fixture(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--json", "profile", "build", "--from-orcid", ORCID_DEMO]) == ExitCode.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["seeds"] == 3 and payload["channels"] >= 1 and payload["journals"] >= 1
    profile = _profile(home)
    assert {s.doi for s in profile.seeds} == {
        "10.0000/example.1",
        "10.0000/example.2",
        "10.0000/example.5",
    }
    assert all(s.confidence == "high" for s in profile.seeds)
    report = (home / "profile-report.md").read_text("utf-8")
    assert f"ORCID {ORCID_DEMO}" in report and "Short note" in report  # not queried, listed
    assert main(["profile", "build", "--from-orcid", "not-an-orcid"]) == ExitCode.USAGE


def test_build_from_openalex_author_fixture(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--json", "profile", "build", "--from-openalex", "A5000000001"]) == ExitCode.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["seeds"] == 3 and payload["foundational"] >= 1
    profile = _profile(home)
    assert all(
        a.openalex_id != "A5000000001" for a in profile.authors.follow
    )  # the person themself
    assert main(["profile", "build", "--from-openalex", "W1"]) == ExitCode.USAGE


def test_live_flag_selects_the_shared_client(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--live`` builds the one shared client (``http.client.from_config``, with the OpenAlex
    budget); without it the recorded fixtures answer."""
    import argparse

    from kallimachos import cli_profile
    from kallimachos.config.schema import Config
    from kallimachos.http.client import Http
    from kallimachos.paths import DataLayout

    monkeypatch.setenv("OFFLINE", "1")
    live = cli_profile.http_client(argparse.Namespace(live=True), Config(), DataLayout(home))
    assert isinstance(live, Http) and live.offline
    assert live.budget is not None
    assert live.budget.ledger_path == DataLayout(home).budget_dir / "openalex.jsonl"
    offline = cli_profile.http_client(argparse.Namespace(live=False), Config(), DataLayout(home))
    assert offline.get("https://api.openalex.org/works/doi:10.0000/example.1") is not None


def test_build_dry_run(home: Path) -> None:
    assert main(["--dry-run", "profile", "build", "--qa", str(ANSWERS)]) == ExitCode.OK
    assert not (home / "profile.yaml").exists()


def test_approve_keeps_user_comments_and_writes_a_backup(home: Path) -> None:
    assert main(["init", "--answers", str(ANSWERS)]) == ExitCode.OK
    path = DataLayout(home).profile_file
    text = path.read_text(encoding="utf-8")
    assert "status: draft" in text
    path.write_text("# my profile, edited by hand\n" + text, encoding="utf-8")
    assert main(["profile", "approve"]) == ExitCode.OK
    after = path.read_text(encoding="utf-8")
    assert after.startswith("# my profile, edited by hand\n")
    assert "status: approved" in after and "approved_at: '" in after
    assert _profile(home).is_approved
    backups = list(path.parent.glob("profile.yaml.bak-*"))
    assert len(backups) == 1 and "status: draft" in backups[0].read_text(encoding="utf-8")


def test_show_accepts_json_after_the_command(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init", "--answers", str(ANSWERS)]) == ExitCode.OK
    capsys.readouterr()
    assert main(["profile", "show", "--json"]) == ExitCode.OK
    assert json.loads(capsys.readouterr().out)["status"] == "draft"
