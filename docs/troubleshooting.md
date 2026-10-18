# Troubleshooting

Start with `kallimachos doctor` (add the global `--json` flag to parse it). Every line is `ok`,
`FAIL` or `fixed` with a reason. `kallimachos doctor --fix` only creates missing data folders,
removes stale locks, deletes cache entries that still carry a key or e-mail in their URL (written
by older versions) and tightens data-folder permissions to owner-only; it never touches anything
else.

## Exit codes

| Code | Meaning | Typical action |
|---|---|---|
| 0 | success | none |
| 1 | unexpected error, or `doctor` found a problem | read the message, run `doctor` |
| 2 | invalid config or profile, usage error | `kallimachos config validate`, fix the named key |
| 3 | partial success (a source or channel failed, result written) | `doctor` shows `source errors`; usually transient |
| 4 | another run holds the lock | wait; a stale lock is removed by `doctor --fix` |
| 5 | approval required | read what the command printed, decide, re-run with `--yes` |
| 6 | network or budget exhausted, no result | wait for the reset (OpenAlex: midnight UTC) |

## Common cases

**No message this morning.** `kallimachos schedule status` shows whether the job ran and its exit
code. Exit 5 at run time usually means the profile is not approved or the Markdown daily file
already existed without Kallimachos markers (approve the touch once with
`kallimachos --yes run daily --rerun`). If the machine was off, `kallimachos schedule run-missed`
sends one merged message.

**`schedule install` exits 5.** Either the user has not approved the files yet (re-run with
`--yes` after they agree) or the e-mail channel is not confirmed:
`kallimachos notify test --channel email`, check the inbox (and spam), then
`kallimachos notify confirm-email` (refused with exit 2 until a test mail has been sent).

**A scan failed with "no source answered" (exit 6).** The run file is still written and `doctor`
shows `last run: ... failed`; the next `run daily` of the same day recomputes it without
`--rerun`. To reproduce a run without a network: `OFFLINE=1
KALLIMACHOS_HTTP_FIXTURES=<dir> kallimachos run daily` replays recorded exchanges in the
`tests/fixtures` format.

**macOS: launchd job does not start.**
`launchctl print gui/$(id -u)/io.github.oselcukk.kallimachos.daily` shows the state and the last
exit code; logs are in `<data>/logs/launchd-daily.err`. Moving or rebuilding the virtual environment
breaks the interpreter path: run `schedule install` again.

**Windows: task did not run.** `schtasks /Query /TN \Kallimachos\daily /V /FO LIST`. A missed
task starts about 10 minutes after the computer is available. Tasks run only while you are
logged on (interactive token).

**Linux: timer did not run while logged out.** Enable lingering once: `loginctl enable-linger`.
`systemctl --user list-timers 'kallimachos-*'` shows the next and last runs.

**Hermes job silent.** Check the job output under `~/.hermes/cron/output/`; the script must be in
`~/.hermes/scripts/` and executable. Re-create the job with the command printed by
`schedule install --backend hermes`.

**GitHub workflow stopped.** In a public repository GitHub disables schedules after 60 days of
inactivity; use a private copy, or re-enable the workflow in the Actions tab.

**An agent says "Kallimachos guard: ... do not edit it with file tools".** That is the optional
PreToolUse hook doing its job. Use the command it names. If the guard is wrong about a file,
report it; `hooks.guard: false` in `config.yaml` turns it off.

**Session summary missing.** `kallimachos hooks status` shows where hooks are installed; Codex
runs new hooks only after you trust them with `/hooks`. `doctor` runs both hook commands once.

**State file error (`state: <file>:<line> ...`).** A state file was edited by hand or truncated.
Do not repair it by hand; show the line and restore from a backup if there is one.

**Avenox literature folder not indexed.** Notes are written but Avenox will not find them; add
the literature folder to the vault's indexed folders following the vault's own instructions.
