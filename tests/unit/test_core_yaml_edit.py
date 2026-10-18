"""Targeted YAML edits keep comments and layout; unsafe shapes fall back (``None``)."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import yaml

from kallimachos.config import load as config_load
from kallimachos.config.schema import Config
from kallimachos.config.yaml_edit import scalar_text, set_scalars

TEXT = """# my config (hand edited)
language: en   # keep english
notify:
  channels:
  - markdown
  - email
  email:
    smtp_host: smtp.example.org  # work account
    confirmed_at: null
  # trailing comment inside notify
timezone: Europe/Example_City
"""


def test_replace_scalar_keeps_comments_and_layout() -> None:
    out = set_scalars(TEXT, {("notify", "email", "confirmed_at"): "2026-10-18T10:00:00+00:00"})
    assert out is not None
    assert "# my config (hand edited)" in out and "# work account" in out
    assert "# trailing comment inside notify" in out
    assert "    confirmed_at: '2026-10-18T10:00:00+00:00'\n" in out
    assert (
        out.replace("    confirmed_at: '2026-10-18T10:00:00+00:00'\n", "    confirmed_at: null\n")
        == TEXT
    )
    assert yaml.safe_load(out)["notify"]["email"]["confirmed_at"] == "2026-10-18T10:00:00+00:00"


def test_insert_missing_key_in_block_mapping() -> None:
    text = TEXT.replace("    confirmed_at: null\n", "")
    out = set_scalars(text, {("notify", "email", "confirmed_at"): "x"})
    assert out is not None
    assert "    smtp_host: smtp.example.org  # work account\n    confirmed_at: x\n" in out
    top = set_scalars(TEXT, {("status",): "approved"})
    assert top is not None and top.endswith("timezone: Europe/Example_City\nstatus: approved\n")


def test_comment_after_value_and_quoted_values_survive() -> None:
    text = "status: draft  # set by approve\napproved_at: null\n"
    out = set_scalars(
        text, {("status",): "approved", ("approved_at",): "2026-01-01T00:00:00+00:00"}
    )
    assert out == "status: approved  # set by approve\napproved_at: '2026-01-01T00:00:00+00:00'\n"


def test_unsafe_shapes_return_none() -> None:
    assert (
        set_scalars("notify: {email: {confirmed_at: null}}\n", {("notify", "email", "x"): 1})
        is None
    )
    assert set_scalars("a: 1\n", {("notify", "email", "confirmed_at"): 1}) is None  # parent missing
    assert set_scalars("notes: |\n  block\n", {("notes",): "x"}) is None
    assert set_scalars("a:\n  b: 1\n", {("a",): "x"}) is None  # the value is a mapping


def test_scalar_text_spellings() -> None:
    assert scalar_text(None) == "null"
    assert scalar_text(True) == "true"
    assert scalar_text("approved") == "approved"
    assert scalar_text("2026-10-18T10:00:00+00:00") == "'2026-10-18T10:00:00+00:00'"
    assert scalar_text("a: b") == "'a: b'"


def test_update_config_values_edits_in_place_with_backup(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "# hand written\nnotify:\n  channels: [markdown, email]\n  email:\n"
        "    smtp_host: smtp.example.org\n    from: reader@example.org\n"
        "    to: [reader@example.org]\n    password_ref: env:SMTP_PW\n"
        "    confirmed_at: null  # until the test mail arrives\n",
        encoding="utf-8",
    )
    config, _layout = config_load.load_config(path)
    config.notify.email.confirmed_at = _dt.datetime(2026, 10, 18, 10, 0, tzinfo=_dt.UTC)
    backup = config_load.update_config_values(
        path, config, {"notify.email.confirmed_at": "2026-10-18T10:00:00+00:00"}
    )
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# hand written\n") and "# until the test mail arrives" in text
    assert "channels: [markdown, email]" in text  # flow sequence left alone
    assert backup is not None and backup.exists() and "confirmed_at: null" in backup.read_text()
    reloaded, _l = config_load.load_config(path)
    assert reloaded.notify.email.confirmed_at == config.notify.email.confirmed_at


def test_update_config_values_falls_back_to_a_full_write(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("notify: {email: {smtp_host: smtp.example.org}}\n", encoding="utf-8")
    config, _layout = config_load.load_config(path)
    config.notify.email.confirmed_at = _dt.datetime(2026, 10, 18, 10, 0, tzinfo=_dt.UTC)
    config_load.update_config_values(
        path, config, {"notify.email.confirmed_at": "2026-10-18T10:00:00+00:00"}
    )
    reloaded, _l = config_load.load_config(path)
    assert reloaded.notify.email.confirmed_at == config.notify.email.confirmed_at
    assert isinstance(reloaded, Config)
    assert config_load.update_config_values(tmp_path / "new.yaml", Config(), {}) is None
    assert (tmp_path / "new.yaml").exists()
