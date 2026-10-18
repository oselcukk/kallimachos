"""config load / validate / migrate and the `config` commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from kallimachos.cli import main
from kallimachos.config.load import (
    check_config_data,
    effective_config,
    load_config,
    read_yaml,
    write_config,
)
from kallimachos.config.migrate import migrate
from kallimachos.config.schema import Config
from kallimachos.config.validate import find_secret_like_values, validate_config
from kallimachos.interfaces import ConfigError, ExitCode

TOKEN = "Qx7" + "k2" * 12  # token-like, built at runtime


def write(path: Path, data: dict[str, Any] | str) -> Path:
    text = data if isinstance(data, str) else yaml.safe_dump(data)
    path.write_text(text, encoding="utf-8")
    return path


# --- read_yaml / load_config -------------------------------------------------------------------


def test_read_yaml_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        read_yaml(tmp_path / "missing.yaml")
    with pytest.raises(ConfigError):
        read_yaml(write(tmp_path / "list.yaml", "- a\n- b\n"))
    with pytest.raises(ConfigError, match="invalid YAML"):
        read_yaml(write(tmp_path / "bad.yaml", "a: [\n"))
    assert read_yaml(write(tmp_path / "empty.yaml", "")) == {}


def test_load_config_defaults_when_missing(kallimachos_home: Path) -> None:
    config, layout = load_config()
    assert config == Config()
    assert layout.root == kallimachos_home.resolve()


def test_load_config_data_dir_precedence(tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    path = write(tmp_path / "c.yaml", {"data_dir": str(other)})
    assert load_config(path)[1].root == other.resolve()
    cli = tmp_path / "cli"
    assert load_config(path, data_dir=str(cli))[1].root == cli.resolve()


def test_example_config_validates(repo_root: Path) -> None:
    config, _ = load_config(repo_root / "config.example.yaml")
    assert config.schema_version == 1
    # the CI step: `kallimachos config validate config.example.yaml`
    assert main(["config", "validate", str(repo_root / "config.example.yaml")]) == ExitCode.OK


@pytest.fixture
def kallimachos_home(_isolated_home: Path) -> Path:
    return _isolated_home


# --- dead keys, secrets, floors, avenox --------------------------------------------------------


def test_dead_key_is_exit_2(tmp_path: Path) -> None:
    path = write(tmp_path / "c.yaml", {"sources": {"arxiv": {"enabled": True, "speed": 9}}})
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert exc.value.exit_code == ExitCode.USAGE
    assert "speed" in str(exc.value)
    assert main(["config", "validate", str(path)]) == ExitCode.USAGE


def test_token_outside_ref_is_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path / "c.yaml", {"notify": {"email": {"username": TOKEN}}})
    assert main(["config", "validate", str(path)]) == ExitCode.USAGE
    err = capsys.readouterr().err
    assert "notify.email.username" in err
    assert TOKEN not in err


def test_token_as_ref_value_is_rejected_but_refs_pass(tmp_path: Path) -> None:
    bad = check_config_data({"sources": {"openalex": {"api_key_ref": TOKEN}}})
    assert any("api_key_ref" in e for e in bad.report.errors)
    good = check_config_data({"sources": {"openalex": {"api_key_ref": "env:OPENALEX_API_KEY"}}})
    assert good.report.ok


def test_find_secret_like_values_paths() -> None:
    data = {"a": {"b_ref": TOKEN, "c": [1, TOKEN]}, "d": "fine"}
    assert find_secret_like_values(data) == ["a.c[1]"]


def test_interval_below_floor_is_exit_2(tmp_path: Path) -> None:
    path = write(tmp_path / "c.yaml", {"sources": {"arxiv": {"min_interval_s": 1.0}}})
    assert main(["config", "validate", str(path)]) == ExitCode.USAGE
    with pytest.raises(ConfigError, match=r"sources\.arxiv\.min_interval_s"):
        load_config(path)


def test_pubmed_keyed_floor() -> None:
    keyed = {"sources": {"pubmed": {"min_interval_s": 0.1, "api_key_ref": "env:NCBI_KEY"}}}
    assert check_config_data(keyed).report.ok
    unkeyed = {"sources": {"pubmed": {"min_interval_s": 0.1}}}
    assert not check_config_data(unkeyed).report.ok


def test_avenox_target_requires_beyin(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    data = {"notes": {"target": "avenox", "vault_path": str(vault)}}
    path = write(tmp_path / "c.yaml", data)
    assert main(["config", "validate", str(path)]) == ExitCode.USAGE
    (vault / "beyin.py").write_text("# stub\n", encoding="utf-8")
    assert main(["config", "validate", str(path)]) == ExitCode.OK
    no_vault = check_config_data({"notes": {"target": "avenox"}})
    assert not no_vault.report.ok


def test_literature_folder_in_avenox_area_is_a_warning() -> None:
    check = check_config_data({"notes": {"literature_folder": "knowledge/papers"}})
    assert check.report.ok and check.report.warnings
    assert not check_config_data({"notes": {"literature_folder": "../outside"}}).report.ok


def test_timezone_and_schedule_times() -> None:
    assert not check_config_data({"timezone": "Not/AZone"}).report.ok
    assert check_config_data({"timezone": "UTC"}).report.ok
    assert not check_config_data({"schedule": {"daily_at": "25:00"}}).report.ok


def test_privacy_values_are_fixed() -> None:
    check = check_config_data({"privacy": {"personal_data_in_logs": True}})
    assert not check.report.ok


def test_validate_config_on_default_is_clean() -> None:
    report = validate_config(Config())
    assert report.ok and report.warnings == []


# --- migrate -----------------------------------------------------------------------------------


def test_migrate_applies_steps_in_order() -> None:
    def v1_to_v2(d: dict[str, Any]) -> dict[str, Any]:
        d["renamed"] = d.pop("old", None)
        return d

    def v2_to_v3(d: dict[str, Any]) -> dict[str, Any]:
        d["added"] = True
        return d

    original = {"schema_version": 1, "old": "x"}
    out, notes = migrate(original, {1: v1_to_v2, 2: v2_to_v3}, 3)
    assert out == {"schema_version": 3, "renamed": "x", "added": True}
    assert notes == ["schema_version 1 -> 2", "schema_version 2 -> 3"]
    assert original == {"schema_version": 1, "old": "x"}


def test_migrate_errors() -> None:
    with pytest.raises(ConfigError, match="newer"):
        migrate({"schema_version": 5}, {}, 1)
    with pytest.raises(ConfigError, match="no migration"):
        migrate({"schema_version": 1}, {}, 2)
    with pytest.raises(ConfigError):
        migrate({"schema_version": "one"}, {}, 1)
    out, notes = migrate({}, {}, 1)
    assert out == {"schema_version": 1} and notes


def test_config_migrate_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import kallimachos.cli_config as cli_config
    import kallimachos.config.load as config_load
    from kallimachos.config.migrate import CONFIG_MIGRATIONS

    path = write(tmp_path / "c.yaml", {"schema_version": 1, "language": "tr"})
    assert main(["config", "migrate", str(path)]) == ExitCode.OK  # already current
    assert not list(tmp_path.glob("*.bak-*"))

    # pretend a schema_version 2 exists whose migration adds a key
    def v1_to_v2(d: dict[str, Any]) -> dict[str, Any]:
        d["timezone"] = "UTC"
        return d

    monkeypatch.setattr(cli_config, "CONFIG_SCHEMA_VERSION", 2)
    monkeypatch.setattr(config_load, "CONFIG_SCHEMA_VERSION", 2)
    monkeypatch.setitem(CONFIG_MIGRATIONS, 1, v1_to_v2)
    before = path.read_text(encoding="utf-8")
    assert main(["--dry-run", "config", "migrate", str(path)]) == ExitCode.OK
    assert path.read_text(encoding="utf-8") == before
    assert main(["config", "migrate", str(path)]) == ExitCode.OK
    assert read_yaml(path) == {"schema_version": 2, "language": "tr", "timezone": "UTC"}
    assert (tmp_path / "c.yaml.bak-v1").read_text(encoding="utf-8") == before


# --- show / write ------------------------------------------------------------------------------


def test_config_show_json_keeps_refs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path / "c.yaml", {"sources": {"openalex": {"api_key_ref": "env:OA_KEY"}}})
    assert main(["--json", "config", "show", str(path)]) == ExitCode.OK
    data = json.loads(capsys.readouterr().out)
    assert data["config"]["sources"]["openalex"]["api_key_ref"] == "env:OA_KEY"
    assert main(["config", "show", str(path)]) == ExitCode.OK
    assert "api_key_ref: env:OA_KEY" in capsys.readouterr().out


def test_write_config_round_trip(tmp_path: Path) -> None:
    config = check_config_data(
        {"language": "tr", "notify": {"email": {"from": "sender@example.org"}}}
    ).config
    assert config is not None
    target = tmp_path / "out" / "config.yaml"
    write_config(target, config)
    back, _ = load_config(target)
    assert back == config
    assert effective_config(back)["notify"]["email"]["from"] == "sender@example.org"


def test_write_config_refuses_invalid(tmp_path: Path) -> None:
    config = Config()
    config.sources.arxiv.min_interval_s = 0.1
    with pytest.raises(ConfigError):
        write_config(tmp_path / "c.yaml", config)
    assert not (tmp_path / "c.yaml").exists()


def test_validate_command_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path / "c.yaml", {"language": "en"})
    assert main(["--json", "config", "validate", str(path)]) == ExitCode.OK
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and data["errors"] == []


def test_validate_command_without_file_uses_defaults(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "validate"]) == ExitCode.OK
    assert "defaults" in capsys.readouterr().out
