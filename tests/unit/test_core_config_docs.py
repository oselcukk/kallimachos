"""``kallimachos config docs``: the key / type / default table comes from the schema."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kallimachos.cli import main
from kallimachos.cli_config import render_config_docs, schema_rows
from kallimachos.config.schema import Config
from kallimachos.interfaces import ExitCode, json_schema


def test_rows_cover_every_leaf_of_the_schema() -> None:
    schema = json_schema(Config)
    rows = schema_rows(schema, schema["$defs"])
    keys = {k for k, _t, _d, _x in rows}
    assert {"schema_version", "sources.openalex.daily_budget_usd", "notify.email.imap_host"} <= keys
    assert {"schedule.backend", "hooks.summary_max_chars", "privacy.send_only_strong_ids"} <= keys
    by_key = {k: (t, d, x) for k, t, d, x in rows}
    assert by_key["sources.openalex.daily_budget_usd"] == ("number", "0.2", "")
    assert by_key["schedule.backend"][0] == "`local` | `hermes` | `github` | `none`"
    assert by_key["notify.email.imap_host"] == ("string | null", "null", "")
    assert by_key["sources"][0] == "section" and "SourcesConfig(" not in by_key["sources"][2]
    assert "pattern" in by_key["schedule.daily_at"][2]


def test_cli_prints_table_json_and_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "docs"]) == ExitCode.OK
    out = capsys.readouterr().out
    assert out.startswith("# config.yaml keys") and '| `language` | string | `"en"` |' in out
    assert out == render_config_docs()
    assert main(["--json", "config", "docs"]) == ExitCode.OK
    rows = json.loads(capsys.readouterr().out)
    assert any(r["key"] == "archive.sensitive_path_regex" for r in rows)
    target = tmp_path / "config.md"
    assert main(["config", "docs", "--out", str(target)]) == ExitCode.OK
    assert target.read_text(encoding="utf-8") == render_config_docs()
    assert main(["--dry-run", "config", "docs", "--out", str(tmp_path / "none.md")]) == ExitCode.OK
    assert not (tmp_path / "none.md").exists()
