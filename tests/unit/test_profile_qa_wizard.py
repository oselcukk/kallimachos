"""Profile: Q&A profile, answers.yaml validation and the wizard materialisation."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from kallimachos.interfaces import ConfigError, build, dump
from kallimachos.paths import DataLayout
from kallimachos.profile.qa import profile_from_answers
from kallimachos.profile.schema import Profile
from kallimachos.profile.wizard import (
    QUESTIONS,
    ask_interactively,
    detect_agent,
    diff_existing,
    load_answers,
    materialize,
    write_yaml_validated,
)

ROOT = Path(__file__).resolve().parents[2]
ANSWERS = ROOT / "tests" / "e2e" / "answers_qa.yaml"
SCHEMAS = ROOT / "schemas"


def _answers() -> dict[str, object]:
    return load_answers(ANSWERS)


# ---------------------------------------------------------------------------
# qa
# ---------------------------------------------------------------------------


def test_profile_from_answers_builds_channels_authors_journals() -> None:
    qa = yaml.safe_load(ANSWERS.read_text("utf-8"))["profile"]["qa"]
    profile, warnings = profile_from_answers(qa, language_hint="en")
    assert profile.status == "draft" and profile.field == "number-theory"
    assert [c.id for c in profile.channels] == ["A", "B"]
    a, b = profile.channels
    assert a.keywords.core[0] == "riemann zeta function" and len(a.keywords.core) == 7
    assert a.negative.exclude == ["zeta potential", "riemann solver"]
    assert a.msc.primary == ["11M06", "11N05"] and a.arxiv.primary == ["math.NT"]
    assert b.name == "Arithmetic geometry" and b.folder == "B-Arithmetic-Geometry"
    assert b.keywords.core == [
        "elliptic curve",
        "modular form",
        "galois representation",
        "rational points",
        "abelian variety",
    ]
    assert b.arxiv.primary == ["math.NT", "math.AG"] and b.msc.primary == ["11G05"]
    assert [j.name for j in a.journals] == [
        "Example Journal of Number Theory",
        "Example Mathematical Proceedings",
    ]
    assert a.journals[0].mode == "full" and a.journals[1].issn == ["0000-0002"]
    assert [x.name for x in profile.authors.always_show] == ["Example Advisor"]
    assert profile.authors.always_show[0].aliases == ["E. Advisor"]
    assert [(f.name, f.channels) for f in profile.authors.follow] == [
        ("Example Researcher One", ["A"]),
        ("Example Researcher Two", []),
    ]
    assert profile.excluded_topics == ["cosmology phenomenology", "control theory"]
    assert warnings == []  # both channels have >= 5 core terms
    # the result is a valid profile.yaml
    schema = json.loads((SCHEMAS / "profile.schema.json").read_text("utf-8"))
    jsonschema.Draft202012Validator(schema).validate(dump(profile))
    assert build(Profile, dump(profile)) == profile


def test_profile_from_answers_warns_on_few_terms_and_rejects_bad_ids() -> None:
    profile, warnings = profile_from_answers(
        {"field": "x", "channels": ["Tiny: one term; two terms"]}, language_hint="tr"
    )
    assert profile.language_hint == "tr" and len(warnings) == 1 and "2 core terms" in warnings[0]
    with pytest.raises(ConfigError):
        profile_from_answers({"channels": [{"id": "bad id!", "name": "n"}]}, language_hint="en")
    with pytest.raises(ConfigError):
        profile_from_answers(
            {"channels": [{"id": "A", "name": "n"}, {"id": "A", "name": "m"}]}, language_hint="en"
        )
    with pytest.raises(ConfigError):
        profile_from_answers({"channels": [{"name": "n", "priority": 9}]}, language_hint="en")
    empty, warnings = profile_from_answers({}, language_hint="en")
    assert empty.channels == [] and any("no channel" in w for w in warnings)


# ---------------------------------------------------------------------------
# answers.yaml
# ---------------------------------------------------------------------------


def test_load_answers_is_strict(tmp_path: Path) -> None:
    good = _answers()
    assert good["language"] == "en" and good["profile"]["methods"] == ["qa"]  # type: ignore[index]
    bad = tmp_path / "answers.yaml"
    bad.write_text("language: en\nnotifications: [markdown]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown key"):
        load_answers(bad)
    bad.write_text("- not\n- a mapping\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_answers(bad)
    bad.write_text("language: en\nmodel: {provider: gpt}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_answers(bad)
    with pytest.raises(ConfigError):
        load_answers(tmp_path / "missing.yaml")


def test_questions_follow_spec_table() -> None:
    numbers = [q.number for q in QUESTIONS]
    assert numbers == [
        "1",
        "2",
        "3",
        "3a",
        "3b",
        "3c",
        "4",
        "5",
        "6",
        "7",
        "8",
        "9",
        "10",
        "11",
        "11b",
        "12",
    ]
    by_number = {q.number: q for q in QUESTIONS}
    assert by_number["1"].default == "en"
    assert (
        by_number["3"].choices == ("qa", "files", "orcid", "openalex") and by_number["3"].multiple
    )
    assert by_number["7"].default == "markdown"
    assert by_number["12"].default == "local"


# ---------------------------------------------------------------------------
# materialize
# ---------------------------------------------------------------------------


def test_materialize_from_acceptance_answers(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path)
    config, profile, notes = materialize(_answers(), layout, env={}, platform="linux")
    assert config.language == "en"
    assert config.sources.arxiv.enabled and config.sources.crossref.enabled
    assert config.sources.openalex.enabled and not config.sources.zbmath.enabled
    assert config.model.provider == "none"
    assert config.notify.channels == ["markdown"] and config.feedback.channels == ["markdown"]
    assert config.schedule.backend == "none" and not config.archive.enabled
    assert profile.status == "draft" and [c.id for c in profile.channels] == ["A", "B"]
    assert not any("next:" in n for n in notes)
    schema = json.loads((SCHEMAS / "config.schema.json").read_text("utf-8"))
    jsonschema.Draft202012Validator(schema).validate(dump(config))


def test_materialize_defaults_and_detection(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "beyin.py").write_text("# stub\n", encoding="utf-8")
    answers = {
        "language": "tr",
        "profile": {
            "methods": ["qa", "files"],
            "qa": {"field": "protein folding and genomics", "channels": ["Folding: a; b; c; d; e"]},
            "files": ["~/Papers"],
        },
        "notify": {
            "channels": ["markdown", "email"],
            "email": {
                "smtp_host": "smtp.example.org",
                "to": ["user@example.org"],
                "password_ref": "keyring:kallimachos/smtp",
            },
        },
        "process": {"enabled": True},
        "notes": {"vault_path": str(vault)},
        "keys": {"openalex": "env:OPENALEX_API_KEY"},
    }
    config, profile, notes = materialize(
        answers, DataLayout(tmp_path), env={"CLAUDECODE": "1"}, platform="darwin"
    )
    assert config.sources.pubmed.enabled and config.sources.biorxiv.enabled
    assert not config.sources.arxiv.enabled
    assert config.model.provider == "claude"
    assert config.feedback.channels == ["markdown", "imap"]
    assert config.notes.target == "avenox" and config.notes.vault_path == str(vault)
    assert config.sources.openalex.api_key_ref == "env:OPENALEX_API_KEY"
    assert profile.channels[0].name == "Folding"
    assert any(n.startswith("next: kallimachos profile build --from-files") for n in notes)
    assert any("launchd" in n for n in notes)


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"keys": {"openalex": "sk-literal-not-a-reference"}}, "references"),
        ({"notify": {"channels": ["telegram"]}}, "bot_token_ref"),
        ({"schedule": {"daily_at": "7:30"}}, "HH:MM"),
        ({"sources": ["arxiv", "scholar"]}, "unknown source"),
        ({"archive": {"enabled": True}}, "archive.root"),
        ({"notes": {"target": "avenox", "vault_path": "/nonexistent/vault"}}, "beyin.py"),
        ({"bridge": {"enabled": True}}, "bridge.remote"),
    ],
)
def test_materialize_rejects_invalid_answers(
    tmp_path: Path, patch: dict[str, object], message: str
) -> None:
    answers: dict[str, object] = {
        "language": "en",
        "profile": {"methods": ["qa"], "qa": {"field": "x"}},
    }
    answers.update(patch)
    with pytest.raises(ConfigError, match=message):
        materialize(answers, DataLayout(tmp_path), env={})


def test_detect_agent() -> None:
    assert detect_agent({}) == "none"
    assert detect_agent({"CODEX_HOME": "x"}) == "codex"
    assert detect_agent({"HERMES_HOME": "x"}) == "hermes"


# ---------------------------------------------------------------------------
# diff / write / interactive
# ---------------------------------------------------------------------------


def test_write_yaml_validated_and_diff(tmp_path: Path) -> None:
    layout = DataLayout(tmp_path)
    config, profile, _ = materialize(_answers(), layout, env={})
    write_yaml_validated(layout.config_file, dump(config), "config.schema.json")
    write_yaml_validated(layout.profile_file, dump(profile), "profile.schema.json")
    assert not list(tmp_path.glob("*.tmp"))
    assert diff_existing(layout, config, profile) == []
    config.language = "tr"
    profile.channels[0].priority = 2
    diffs = diff_existing(layout, config, profile)
    assert "config: language: 'en' -> 'tr'" in diffs
    assert any(d.startswith("profile: channels[0].priority: 1 -> 2") for d in diffs)
    with pytest.raises(ConfigError):
        write_yaml_validated(tmp_path / "bad.yaml", {"status": "nope"}, "profile.schema.json")
    assert not (tmp_path / "bad.yaml").exists()


def test_ask_interactively_scripted() -> None:
    replies = iter(
        [
            "tr",  # 1 language
            "",  # 2 data dir
            "qa files",  # 3 methods
            "Sieves: large sieve; selberg sieve; brun sieve; sieve weights",  # 3a line 1
            "",  # 3a end
            "~/Papers, ~/refs.bib",  # 3b
            "arxiv crossref",  # 4
            "openalex=env:OPENALEX_API_KEY",  # 5
            "none",  # 6
            "",  # 7 notify (default markdown)
            "",  # 8 feedback
            "n",  # 9 archive
            "y",  # 10 process
            "plain",  # 11 notes
            "n",  # 11b bridge
            "none",  # 12 schedule
        ]
    )
    answers = ask_interactively(
        "en", input_fn=lambda _prompt: next(replies), output=lambda _t: None
    )
    assert answers["language"] == "tr"
    assert answers["profile"]["methods"] == ["qa", "files"]
    assert answers["profile"]["files"] == ["~/Papers", "~/refs.bib"]
    assert answers["profile"]["qa"]["channels"][0].startswith("Sieves:")
    assert answers["sources"] == ["arxiv", "crossref"]
    assert answers["keys"]["openalex"] == "env:OPENALEX_API_KEY"
    assert answers["process"]["enabled"] is True and answers["archive"]["enabled"] is False
    assert answers["notes"]["target"] == "plain" and answers["schedule"]["backend"] == "none"
    assert answers["feedback"]["channels"] is None
