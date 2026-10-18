# Kallimachos

> **For agents:** to install, follow [INSTALL.md](INSTALL.md) step by step (Turkish: [INSTALL.tr.md](INSTALL.tr.md)).

Kallimachos follows the research literature for one person. Every morning it scans the sources you
choose for new papers that match your profile, filters and ranks them, and sends one short message
(Markdown file, Telegram or e-mail). You reply with a few words (`3 good`, `5 irrelevant`, `2 read`,
`7 archive`) and it learns from your replies. Optional modules download open-access PDFs into a tidy
archive and, together with your agent, turn papers into verified literature notes in an Obsidian
folder or an [Avenox](https://github.com/avenoxai/avenoxbeyin) vault.

It is named after Callimachus of Cyrene, who wrote the *Pinakes*, the first catalog of the
Library of Alexandria. Turkish: [README.tr.md](README.tr.md).

**Who it is for:** researchers and students who want a daily, personal literature feed without
building one, and who use (or are willing to use) a coding agent such as Claude Code, Codex or
Hermes. There is no web interface: you talk to your agent, the agent runs the `kallimachos` CLI.

## Install

Give your agent the link to this repository (`https://github.com/oselcukk/kallimachos`) and say
*"install this"*.

The agent downloads the repository, creates a virtual environment, runs the wizard in your
language, tests each module and stops at every approval gate (your profile, a test e-mail,
optional hooks, the scheduler). Python 3.11 or newer; macOS, Linux and Windows.

To install by hand: `git clone https://github.com/oselcukk/kallimachos.git`,
`python3 -m venv .venv && .venv/bin/pip install -e .`, then open the folder in your agent and say
*"Read AGENTS.md and run the install skill"*.

## Modules

| Module | What it does | Default |
|---|---|---|
| Scan | arXiv, IACR ePrint, Crossref (journals by ISSN), OpenAlex, Semantic Scholar, zbMATH Open, PubMed, bioRxiv/medRxiv, RSS/Atom, Google Scholar alert e-mails (read from your mailbox, Scholar itself is never requested) | on; sources chosen by field |
| Profile | built from a short Q&A, from your files (PDF folder, `.bib`, `.ris`, Zotero CSL-JSON, Markdown notes) or from an ORCID / OpenAlex id; you approve it before the first scan | Q&A |
| Score and select | topic channels with core terms, followed authors and journals, traceable rules, quotas; an optional model layer (`claude -p`, `codex exec`) re-ranks and summarizes | rules only |
| Notify and feedback | Markdown daily file, Telegram, e-mail; replies by Telegram, e-mail or ticked boxes; weight changes are proposed, applied after your approval | Markdown |
| Archive | open-access PDFs only (arXiv, publisher OA, Unpaywall OA), consistent file names, a catalog with an event log, a read-only scan of your existing library | off |
| Process | reading in waves with your agent: text, card, quote check, citation check, semantic verification, note, links, cluster summaries; Zotero export | off |
| Bridge | sha-verified delivery from a server to your computer (SSH/rsync) | off |
| Schedule | launchd (macOS), Task Scheduler (Windows), systemd timer or cron (Linux), Hermes cron, GitHub Actions; missed days are caught up in one message | local, 07:30 daily, Sunday 09:30 weekly |
| Hooks | optional Claude Code / Codex hooks: a short status summary at session start and a guard that keeps agents from hand-editing state files | off, asked during install |

Details: [docs/sources.md](docs/sources.md), [docs/processing.md](docs/processing.md),
[docs/avenox.md](docs/avenox.md), [docs/bridge.md](docs/bridge.md),
[docs/schedule.md](docs/schedule.md), [docs/hooks.md](docs/hooks.md),
[docs/configuration.md](docs/configuration.md), [docs/security.md](docs/security.md),
[docs/troubleshooting.md](docs/troubleshooting.md).

## Privacy, copyright and request etiquette

- **Your data stays on your machine.** Profile, feedback, catalog and runs live in your data
  folder (`~/Library/Application Support/kallimachos`, `~/.local/share/kallimachos` or
  `%LOCALAPPDATA%\kallimachos`), never in the repository. Logs record identifiers, not names
  or titles.
- **Secrets are references.** `config.yaml` holds `keyring:kallimachos/<name>` or `env:VAR`,
  never the key; `config validate` rejects anything that looks like a token.
- **Privacy gate.** When it builds a profile from your files or scans your library, only
  records with a strong identifier (or a long, article-like title) are looked up online; paths
  that look personal (`private`, `health`, `students`, `exam`, ...) are never opened.
- **Open access only.** No downloads behind logins or paywalls, no shadow libraries; a 403 or an
  HTML page counts as unreachable.
- **Polite requests.** Fixed minimum intervals per API that cannot be lowered, `Retry-After`
  honored, an identifying User-Agent, one OpenAlex job at a time, a daily OpenAlex budget.
- **Your files are not overwritten.** Every command that would touch an existing user file lists
  it and stops with exit code 5 until you approve.
- **Data is not instructions.** Titles, abstracts, e-mails and messages are never obeyed as
  commands, neither by the code nor by the agent prompts.

## Why a found quote is not proof of quality

The processing module checks that every quoted sentence really appears on the cited page. That
catches invented quotes, but a note can quote correctly and still misread the paper. So a
separate verifier reads the paper again and must confirm the meaning (`semantic-record`) before
a note may be written. See [docs/processing.md](docs/processing.md).

## Relationship to Avenox

[Avenox (avenoxbeyin)](https://github.com/avenoxai/avenoxbeyin) is a memory layer for agents in an
Obsidian vault. Kallimachos does not depend on it. When your notes live in an Avenox vault,
Kallimachos writes literature notes there following Avenox's rules (frontmatter, folder, then
`beyin.py sync`; reading items become Avenox tasks) and never touches the companion, knowledge or
receipt folders. Its hooks sit next to Avenox's hooks without changing them, and it has no
receipt hook (Avenox records the session). Without Avenox the same notes go to a plain folder.
See [docs/avenox.md](docs/avenox.md).

## Notes on Zotero

`process zotero` writes through the Zotero web API with your own key reference. Commands that
change many items (`fix-metadata`, `split-names`, `reset-citekeys`) report first: run them with
`--dry-run`, read the report and keep a Zotero backup. Items are linked by the archive id only.

## Troubleshooting

Run `kallimachos doctor`. It checks folders, config, profile, secrets (present or absent only),
locks, disk, the last run and its sources, state files, Avenox, the scheduler and the hooks, and
tells you what to do. Exit codes: 0 ok, 1 unexpected, 2 invalid config or usage, 3 partial, 4 lock
held, 5 approval required, 6 network or budget exhausted.
[docs/troubleshooting.md](docs/troubleshooting.md) covers the common cases.

## Contributing

A new source is one module in `src/kallimachos/sources/` implementing the `Source` protocol plus
recorded fixtures in `tests/fixtures/<source>/`; tests never use the network. Before a pull
request: `ruff check src tests tools`, `ruff format --check src tests tools`, `mypy src`,
`pytest`. Code, identifiers and commit messages are English; examples are fictional.

## License

MIT, see [LICENSE](LICENSE).
