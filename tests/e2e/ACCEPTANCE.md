# End-to-end acceptance

Manual checklist for a **clean machine** (a fresh user account or VM without a previous
Kallimachos data folder). Run it on macOS and Windows by hand; Linux is covered by CI plus the
same list. Record the date, OS, Python version and commit for every run in the table at the end.

Conventions: `K` is `.venv/bin/kallimachos` (macOS/Linux) or `.venv\Scripts\kallimachos`
(Windows), run from the install folder. Use a throw-away data folder:
`export KALLIMACHOS_HOME=$(mktemp -d)` (PowerShell:
`$env:KALLIMACHOS_HOME = New-Item -ItemType Directory (Join-Path $env:TEMP kallimachos-e2e)`).
Offline steps use `OFFLINE=1` (recorded fixtures only). Automated parts: `pytest` (offline) and
`KALLIMACHOS_E2E_NETWORK=1 pytest -m e2e_network tests/e2e` (Avenox, needs the internet).

## 0. Install from the entry point

- [ ] Give an agent only the link to `INSTALL.md` (and once `INSTALL.tr.md`) and say
      "install this" / "bunu kur". The agent downloads (git and, once, the zip route), creates the
      venv, runs `doctor`, asks the wizard questions in the user's language and stops at every
      gate (profile, test mail, hooks, schedule). No file outside the install and data folders is
      written before a yes.
- [ ] `K --version`, `K doctor` before the wizard: only `config`/`profile` missing.

## 1. Three profile paths (each OS)

1. **Q&A**
   - [ ] `K init --answers tests/e2e/answers_qa.yaml` -> `config.yaml`, draft `profile.yaml`.
   - [ ] `K profile show` -> table; `K run daily --first-run --no-model` exits **5** before approval.
   - [ ] `K profile approve`; `OFFLINE=1 K run daily --first-run --no-model` -> Markdown daily file
         created, `runs/<id>.json` validates.
   - [ ] `K feedback add` for two items; `K feedback apply-weights --dry-run` prints a report.
2. **Files**
   - [ ] `K profile build --from-files tests/e2e/sample_library/` -> at least 2 seeds, 1 channel
         proposal, 1 journal proposal.
   - [ ] privacy gate: nothing under `sample_library/private/` is opened or queried (request log
         empty for those files).
3. **ORCID**
   - [ ] `K profile build --from-orcid 0000-0002-1825-0097` (fixture) -> seeds -> profile.
   - [ ] optional: the same with `--live` (network).

## 2. Notification channels

- [ ] Markdown: existing daily file without markers -> exit 5, nothing written; with `--yes` the
      marked block is inserted and the user's text is untouched; a rerun is idempotent.
- [ ] Telegram: `K notify test --channel telegram` arrives (plain text); a reply `2 good` is
      picked up by `K feedback poll`.
- [ ] E-mail: `K notify test --channel email` arrives with `[kallimachos <run id>]` in the
      subject; **before** `K notify confirm-email`, `K schedule install` exits **5**; after it,
      install proceeds to the approval prompt.

## 3. Scheduling

- [ ] `K schedule install` prints every file and exits **5**; nothing is written.
- [ ] `K --yes schedule install`; `K schedule status` lists the jobs.
  - macOS: `plutil -lint ~/Library/LaunchAgents/io.github.oselcukk.kallimachos.*.plist` OK;
    `launchctl print gui/$(id -u)/io.github.oselcukk.kallimachos.daily` shows the job.
  - Windows: `schtasks /Query /TN \Kallimachos\daily /V /FO LIST`; StartWhenAvailable is set.
  - Linux: `systemctl --user list-timers 'kallimachos-*'`; `Persistent=true` in the timer.
- [ ] Put the clock (or `--date`) forward, or skip two days: `K schedule run-missed` sends **one**
      merged message covering the missed days (at most `max_catch_up_days`).
- [ ] `K schedule remove` removes only Kallimachos' jobs; a foreign crontab line survives (Linux
      cron).
- [ ] Hermes (if available): `K schedule install --backend hermes` writes
      `~/.hermes/scripts/kallimachos-*.sh` and prints `hermes skills install` and
      `hermes cron create` commands; running them creates jobs; `jobs.json` is not touched by
      Kallimachos.
- [ ] GitHub (private test repository): `K schedule install --backend github --repo-dir <clone>`
      writes `.github/workflows/kallimachos-scan.yml`; with the secrets set a manual
      `workflow_dispatch` run succeeds.

## 4. Hooks

- [ ] With an existing `~/.claude/settings.json` that has other hooks (for example an Avenox
      vault's): `K hooks install --target claude --scope user` exits 5 and prints the entries;
      `K --yes hooks install ...` keeps every other hook, writes
      `settings.json.bak-kallimachos-*`; running it twice changes nothing.
- [ ] New Claude Code session: the context contains a `Kallimachos:` summary of at most 600
      characters; the Avenox context is still there.
- [ ] Ask the agent to "fix a line in state/seen.jsonl" -> the guard denies and names the
      command; editing user text outside a note's managed block works; editing inside
      `<!-- kallimachos:begin full_reading -->` is denied.
- [ ] Codex: `K --yes hooks install --target codex --scope user`, trust with `/hooks`, the same
      two checks (the edit arrives as `apply_patch`).
- [ ] `K doctor` reports the hooks as working; `K --yes hooks uninstall ...` restores the file.

## 5. Archive and processing

- [ ] `K archive run --dry-run` writes nothing.
- [ ] `K process extract`, `check`, `cite` on a synthetic PDF; `note-write` without a semantic
      record exits **5**.

## 6. Avenox integration

Automated: `KALLIMACHOS_E2E_NETWORK=1 pytest -m e2e_network tests/e2e/test_avenox_e2e.py`
(`tools/fetch_avenox.py` downloads and sha256-checks the official release ZIP, the official
installer sets up a temporary vault with a temporary HOME). Manual on each OS:

- [ ] fresh vault with the avenoxbeyin installer; `notes.target: avenox`; `K config validate` OK.
- [ ] `note-write` for a synthetic work -> note in the literature folder with the note/1
      frontmatter; `beyin.py sync` succeeds.
- [ ] `beyin.py context "<title words>"` finds the note (record carries `eid`). Observed with
      3.11.0: a query consisting only of the eid abstains, so search by title.
- [ ] `K process queue add <eid> --kind read` + `K process queue sync` -> exactly one task under
      `tasks/`; a second sync creates nothing.
- [ ] the receipt line from the process skill appears in `beyin.py recap`.
- [ ] plain-folder variant: the same steps without Avenox (`notes.target: plain`), queue goes to
      the queue file.

## 7. Repository hygiene

- [ ] `gitleaks detect` clean (CI job `secret scan`).
- [ ] `pytest tests/test_repo_hygiene.py` green; `python tools/sync_skills.py --check` exits 0.

## Runs

| Date | OS | Python | Commit | Paths 1-3 | Channels | Schedule | Hooks | Avenox | Notes |
|---|---|---|---|---|---|---|---|---|---|
| | | | | | | | | | |
