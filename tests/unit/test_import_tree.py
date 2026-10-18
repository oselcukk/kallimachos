"""Every module of the package tree exists and imports; stubs name their owner."""

from __future__ import annotations

import importlib
import pkgutil
import re
from pathlib import Path

import pytest

import kallimachos

SRC = Path(kallimachos.__file__).resolve().parent

SPEC_TREE = [
    "__init__",
    "__main__",
    "cli",
    "version",
    "interfaces",
    "paths",
    "secrets",
    "doctor",
    "config.schema",
    "config.load",
    "config.migrate",
    "config.validate",
    "profile.schema",
    "profile.wizard",
    "profile.qa",
    "profile.from_files",
    "profile.from_orcid",
    "profile.match",
    "profile.journals",
    "profile.importers.markdown",
    "profile.importers.bibtex",
    "profile.importers.ris",
    "profile.importers.csl_json",
    "profile.importers.pdf_meta",
    "http.client",
    "http.budget",
    "http.cache",
    "http.ua",
    "sources.base",
    "sources.registry",
    "sources.arxiv",
    "sources.iacr_eprint",
    "sources.crossref",
    "sources.openalex",
    "sources.semantic_scholar",
    "sources.zbmath",
    "sources.pubmed",
    "sources.biorxiv",
    "sources.rss",
    "sources.scholar_imap",
    "records.model",
    "records.keys",
    "records.pool",
    "records.normalize",
    "records.paratext",
    "scoring.topic",
    "scoring.field",
    "scoring.venue_quality",
    "scoring.rules.base",
    "scoring.rules.core",
    "scoring.rules.loader",
    "scoring.rank",
    "scoring.quota",
    "scoring.seen",
    "model.provider",
    "model.claude_cli",
    "model.codex_cli",
    "model.hermes",
    "model.none",
    "model.select",
    "notify.base",
    "notify.markdown_daily",
    "notify.telegram",
    "notify.email_smtp",
    "notify.format",
    "notify.budget",
    "feedback.grammar",
    "feedback.intake_telegram",
    "feedback.intake_imap",
    "feedback.intake_markdown",
    "feedback.store",
    "feedback.weights",
    "archive.triggers",
    "archive.metadata",
    "archive.oa",
    "archive.download",
    "archive.naming",
    "archive.catalog",
    "archive.taxonomy",
    "archive.existing",
    "process.extract",
    "process.ocr",
    "process.chunk",
    "process.card",
    "process.quote",
    "process.check",
    "process.cite",
    "process.semantic",
    "process.status",
    "process.note",
    "process.links",
    "process.notes_index",
    "process.cluster",
    "process.queue",
    "process.avenox",
    "process.zotero.ris",
    "process.zotero.metadata",
    "process.zotero.names",
    "process.zotero.citekey",
    "process.zotero.link",
    "bridge.base",
    "bridge.ssh_rsync",
    "bridge.deliver",
    "state.store",
    "state.lock",
    "state.machine",
    "state.runs",
    "schedule.base",
    "schedule.launchd",
    "schedule.taskscheduler",
    "schedule.systemd",
    "schedule.cron",
    "schedule.hermes",
    "schedule.gh_actions",
    "schedule.catchup",
    "i18n",
]


@pytest.mark.parametrize("module", SPEC_TREE)
def test_spec_module_imports(module: str) -> None:
    importlib.import_module(f"kallimachos.{module}")


def test_all_packages_import() -> None:
    names = [m.name for m in pkgutil.walk_packages([str(SRC)], prefix="kallimachos.")]
    assert len(names) > 100
    for name in names:
        importlib.import_module(name)


def test_prompt_and_data_files_exist() -> None:
    for rel in (
        "model/prompts/rerank.md",
        "model/prompts/summarize.md",
        "model/prompts/why.md",
        "model/prompts/semantic_check.md",
        "process/prompts/reader.md",
        "process/prompts/recorder.md",
        "process/prompts/verifier.md",
        "process/prompts/cluster.md",
        "i18n/en.json",
        "i18n/tr.json",
    ):
        assert (SRC / rel).is_file(), rel


def test_every_stub_gives_a_reason() -> None:
    pattern = re.compile(r"raise NotImplementedError\((.*)\)")
    bad: list[str] = []
    for path in SRC.rglob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            m = pattern.search(line)
            if m and not m.group(1).strip():
                bad.append(f"{path.relative_to(SRC)}:{lineno}")
    assert not bad, bad
