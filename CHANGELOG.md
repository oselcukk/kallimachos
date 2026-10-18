# Changelog

All notable changes to Kallimachos are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-18

First public release. Kallimachos follows the research literature for one person: it scans the
sources you choose every day, scores new papers against your profile and sends one short message;
your replies teach it what matters. It is driven by your coding agent through the `kallimachos` CLI.

### Added

- Scanning of ten sources: arXiv, IACR ePrint, Crossref (journals by ISSN), OpenAlex, Semantic
  Scholar, zbMATH Open, PubMed, bioRxiv/medRxiv, RSS/Atom and Google Scholar alert e-mails read
  from your mailbox (Scholar itself is never requested).
- Polite, private requests: fixed minimum intervals per API, `Retry-After` honoured, an
  identifying User-Agent, a daily OpenAlex budget, and keys kept out of URLs, caches and logs.
- A research profile built from a short Q&A, from your files (PDF folder, BibTeX, RIS, Zotero
  CSL-JSON, Markdown notes) or from an ORCID / OpenAlex id, with a privacy gate for personal
  paths; you approve the profile before the first scan (`init`, `profile show|approve`).
- Scoring against the profile: topic channels, followed authors and journals, traceable rules,
  ranking with a journal quota, and an optional model layer (Claude, Codex or Hermes) that
  re-ranks and summarizes.
- Daily and weekly runs (`run daily|weekly`) with notifications as a Markdown daily file, a
  Telegram message or an e-mail.
- Feedback by Telegram, e-mail or ticked Markdown boxes (`3 good`, `5 irrelevant`, `7 archive`);
  weight changes are proposed and applied only after your approval.
- An open-access archive: PDFs from arXiv, publisher OA and Unpaywall OA only, consistent file
  names, a catalog with an event log, and a read-only scan of an existing library.
- A reading and notes pipeline run together with your agent: text extraction, cards, quote and
  citation checks, mandatory semantic verification, literature notes in an Obsidian folder or an
  Avenox vault, links, cluster summaries, a reading queue and Zotero export.
- An optional bridge that delivers archived PDFs from a server to your computer with sha
  verification (SSH/rsync).
- Scheduling with launchd, Task Scheduler, systemd timers, cron, Hermes cron or GitHub Actions;
  missed days are caught up in one message.
- Optional Claude Code / Codex hooks: a short status summary at session start and a guard that
  keeps agents from hand-editing state files.
- `doctor` health report with safe `--fix`, `config validate|show|migrate`, English and Turkish
  messages, and approval gates (exit code 5) before any existing user file is touched.
- Documentation (`README.md`, `INSTALL.md`, their Turkish versions and `docs/`) and agent skills
  for install, run, feedback, process and doctor.

[Unreleased]: https://github.com/oselcukk/kallimachos/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/oselcukk/kallimachos/releases/tag/v0.1.0
