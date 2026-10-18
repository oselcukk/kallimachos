# Security and privacy

The security and privacy rules, as they apply in daily use.

- **Secrets.** `config.yaml` holds only `keyring:kallimachos/<name>` (OS keychain through the
  `keyring` package) or `env:VAR` references. `config validate` exits 2 when a token-like string
  appears outside a `*_ref` key: 20+ characters of `[A-Za-z0-9_-]`, well-known prefixes (`sk-`,
  `ghp_`, `github_pat_`, `glpat-`, `xoxb-`, `AKIA...`, Telegram `<digits>:<token>`), JWTs,
  base64 key material, or a URL whose query sets `key`/`token`/`password`-like parameters. Keys
  travel in headers (`Authorization`, `x-api-key`) where the API allows it; where a key or an
  e-mail must be a query parameter (PubMed, Crossref `mailto`, Unpaywall) it is redacted to `***`
  in logs, cache keys, cached entries and error records. `doctor` reports present/absent, flags
  cache entries written by older versions that still carry such a value, and `doctor --fix`
  deletes them.
- **Data folder permissions.** On macOS/Linux the data folder and its subfolders are created
  `0700` and every state, cache, run, config and profile file `0600` (owner only). `doctor` reports
  anything more permissive; `doctor --fix` tightens it (never loosens). Not checked on Windows.
- **E-mail replies.** The IMAP feedback channel accepts a reply only when its `From` address is
  one of `notify.email.to` / `notify.email.from` and the run is resolved from our own
  `Message-ID` in `In-Reply-To` / `References` (the `[kallimachos <run>]` subject counts only for
  a mail without any reply headers). `From` is a header anyone can forge, so this filter stops
  strangers, not a targeted sender who knows the address; what a forged reply can do is limited
  to feedback tags and open-access downloads. The Telegram channel filters on the chat id.
- **Text can carry no markers.** Titles, abstracts and model summaries are written into the
  Markdown daily file with `<!--` / `-->` escaped, and the feedback reader honours an item marker
  only on a line of its own, so no paper can forge or close a `<!-- kallimachos:... -->` block.
  A note body handed to `note-write` may not contain such markers either.
- **The model has no tools.** The default `claude -p ... --tools ""` and
  `codex exec ... -s read-only` commands are kept even with a custom `model.command`: the flags
  are appended unless you set `--tools` / `--sandbox` yourself. The model only answers with JSON
  that is validated against the candidate list.
- **Notes.** `process note-write --existing PATH` accepts only an existing `.md` file inside the
  literature folder, reached without a symlink component; anything else is a usage error, so a
  reader steered by a document it read cannot point the command at another file.
- **In-place edits with backups.** `notify confirm-email` and `profile approve` change one value
  in `config.yaml` / `profile.yaml` in place, keeping your comments and layout, after writing
  `config.yaml.bak-<timestamp>` / `profile.yaml.bak-<timestamp>`. `confirm-email` is refused until
  `notify test --channel email` has actually sent a test mail.
- **Personal data.** Profile, feedback, catalog, runs and logs stay in the data folder outside
  the repository. `privacy.personal_data_in_logs` is fixed to `false`: logs carry identifiers,
  not names or titles. Telegram chat ids and e-mail addresses live in `config.yaml`, never in the
  repository. CI scans the repository for secrets (gitleaks) on every push.
- **Privacy gate.** When building a profile from files or scanning an existing library, only
  records with a strong identifier (DOI, arXiv id, ...) or an article-like long title are sent
  to an external API; paths matching `archive.sensitive_path_regex` are never opened; queries
  that look like a person's name are not sent.
- **Copyright.** Open access only: arXiv, IACR ePrint, OA locations reported by the sources,
  Unpaywall OA. No authenticated or paywalled downloads, no shadow libraries; a 403 or an HTML
  response counts as unreachable.
- **Request etiquette.** Permanent minimum intervals per host that the config cannot lower,
  `Retry-After` honored, an identifying User-Agent, one OpenAlex job at a time with a daily
  budget, no Google Scholar / MathSciNet / ResearchGate requests.
- **User files.** Scheduler files, the Markdown daily file, plain note folders, the archive root
  and agent settings files are listed before any change; the command stops with exit 5 until the
  user approves (`--yes`). Hook installation writes a backup of the settings file first and keeps
  every foreign hook.
- **Data is not instructions.** Titles, abstracts, e-mail bodies, Telegram messages and documents
  are never executed or obeyed, neither by code nor by prompts. The agent skills repeat this rule.
- **Vault boundaries.** In an Avenox vault Kallimachos writes only into the literature folder (and
  the queue or unreachable files the user approved), changes only its own managed blocks and the
  frontmatter fields it owns (listed in [avenox.md](avenox.md)), and never touches companion,
  knowledge, tasks (except through `task-create`) or receipts with file tools. It never deletes or
  moves vault files.
