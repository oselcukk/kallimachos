# Scheduling

`kallimachos schedule install` turns the `schedule` section of `config.yaml` into scheduler files,
**prints every file and the notes, and writes nothing without
`--yes`** (exit 5). With the e-mail channel it also refuses until the test mail has been confirmed
(`notify test --channel email`, then `notify confirm-email`; exit 5).

```yaml
schedule:
  backend: local        # local | hermes | github | none
  daily_at: "07:30"     # local time of the machine
  weekly_on: sun
  weekly_at: "09:30"
  archive_at: "06:30"   # only used when archive.enabled; runs before the daily scan
  catch_up_missed: true
  max_catch_up_days: 3
```

| Command | What it does |
|---|---|
| `kallimachos schedule install [--backend B] [--python PATH] [--repo-dir DIR]` | render, show, and with `--yes` install. `B` is `local`, `hermes`, `github`, or a platform name (`launchd`, `taskscheduler`, `systemd`, `cron`). |
| `kallimachos schedule status` | installed jobs, last run and exit code reported by the scheduler, last successful daily and weekly run |
| `kallimachos schedule remove` (alias `uninstall`) | lists Kallimachos' own jobs; with `--yes` removes them (nothing else) |
| `kallimachos schedule run-missed` | the catch-up job: one merged run for the missed days |

Jobs: `daily` (`run daily`), `weekly` (`run weekly`), `archive` (`archive run`, when the archive
module is on) and `catchup` (`schedule run-missed`, when `catch_up_missed`). Every job runs
`<python> -m kallimachos --data-dir <data folder> ...` with the interpreter of the current
installation, so it does not depend on the scheduler's `PATH`.

## Backends

### macOS: launchd

Files: `~/Library/LaunchAgents/io.github.oselcukk.kallimachos.<job>.plist` with
`StartCalendarInterval`, `RunAtLoad false`, `ProcessType Background`, logs in
`<data>/logs/launchd-<job>.{out,err}`; the `catchup` agent has `RunAtLoad true`. Installed with
`launchctl bootstrap gui/<uid> <plist>`.

`man launchd.plist`: unlike cron, launchd starts a `StartCalendarInterval` job missed during
sleep when the Mac wakes, and several missed intervals coalesce into one event. Behavior for a
powered-off Mac is not documented, which is why the catch-up agent exists.

### Windows: Task Scheduler

XML files in `<data>/schedule/taskscheduler/` (UTF-16), registered with
`schtasks /Create /TN \Kallimachos\<job> /XML <file> /F`. Settings:
`StartWhenAvailable=true` (a missed start runs when the computer is available again; Microsoft
documents a default delay of about 10 minutes), `RunOnlyIfNetworkAvailable`,
`MultipleInstancesPolicy IgnoreNew`, one hour limit. The catch-up task has a `LogonTrigger`
(2 minutes after logon). `pythonw.exe` is used when it sits next to `python.exe`, so no console
window opens. Status comes from `schtasks /Query /V /FO LIST` (English labels; a localized
Windows may show no last-run details).

### Linux: systemd user timers (cron fallback)

Units in `~/.config/systemd/user/`: `kallimachos-<job>.service` (`Type=oneshot`) and
`kallimachos-<job>.timer` with `OnCalendar=` and `Persistent=true` (systemd.timer(5): when the
timer is activated again it fires immediately if a run was missed while it was inactive). The
catch-up service is wanted by `default.target` (runs when your user session starts). Installed
with `systemctl --user daemon-reload` and `systemctl --user enable --now <timer>`. User timers
run only while you are logged in unless lingering is on: `loginctl enable-linger`.

Without systemd the backend is cron: a block between `# BEGIN kallimachos` and
`# END kallimachos` in your crontab (the rest of the crontab is kept), with an `@reboot` line for
`run-missed`. cron skips runs while the machine is off.

### Hermes cron

`--backend hermes` writes `~/.hermes/scripts/kallimachos-<job>.sh` (Hermes accepts only a script
*file* under `$HERMES_HOME/scripts/` for `--script`, not a command line) and prints the commands
to run once:

```sh
hermes skills install <repo>/.agents/skills/run/SKILL.md   # or skills.external_dirs in ~/.hermes/config.yaml
hermes cron create "30 7 * * *" "<prompt>" --skill kallimachos-run --script kallimachos-daily.sh --deliver telegram --name kallimachos-daily
hermes cron create "30 9 * * 0" "<prompt>" --skill kallimachos-run --script kallimachos-weekly.sh --deliver telegram --name kallimachos-weekly
hermes cron create "30 6 * * *" "" --no-agent --script kallimachos-archive.sh --deliver telegram --name kallimachos-archive
```

The daily and weekly scripts run `kallimachos --json run ... --no-notify`: in agent mode the
script's stdout is appended to the prompt and the agent writes the message following the run
skill. Hermes delivers the message, not a Kallimachos channel: `select` records the selection and
marks those papers as shown, so they do not come back the next day. The archive job uses `--no-agent` (stdout delivered as is; empty stdout is silent).
`~/.hermes/cron/jobs.json` is never written by Kallimachos (`schedule status` only reads it).
Hermes catches up a missed run only within half the period, between 2 minutes and 2 hours; after
a longer outage run `kallimachos-catchup.sh` (for example at boot).

### GitHub Actions

`--backend github --repo-dir <checkout>` writes `.github/workflows/kallimachos-scan.yml` into
that checkout (an example is in `.github/workflows/scan.yml.example`). Facts from
docs.github.com ("Events that trigger workflows"):

- the shortest interval is 5 minutes, runs can be delayed under load and the start of every hour
  is the busiest time (pick a minute such as `:30`); there is no guaranteed start time;
- scheduled workflows run only from the **default branch**;
- in a **public** repository, scheduled workflows are disabled after 60 days without repository
  activity. Because the workflow runs with your profile, use a **private** copy of the
  repository;
- `workflow_dispatch` adds a manual "Run workflow" button.

GitHub cron is UTC; `schedule install` converts your local times with today's offset (re-render
after a daylight-saving change). Config and profile never enter the repository: store them as
the secrets `KALLIMACHOS_CONFIG_B64` and `KALLIMACHOS_PROFILE_B64`
(`base64 < config.yaml | gh secret set KALLIMACHOS_CONFIG_B64`), plus `TELEGRAM_BOT_TOKEN`,
`SMTP_PASSWORD`, `OPENALEX_API_KEY` as needed; in that config use `env:VAR` references. State
(`seen`, `runs`, `weights.json`) is kept with `actions/cache`, which is best effort. Use the
Telegram or e-mail channel there (a Markdown file on the runner is lost) and `model.provider: none`
unless you install a model CLI in the workflow.

## Missed runs (`run-missed`)

`run-missed` looks at the last successful daily run. If runs are missing up to the most recent
due day (today once `daily_at` has passed, otherwise yesterday), it starts **one** daily run for
that day whose window covers every missed day, at most `max_catch_up_days` back, so you get one
merged message instead of several. Nothing happens when no run ever succeeded (the first run is
`run daily --first-run`), when the due day already has a run, or when `catch_up_missed: false`.
The weekly scan is caught up the same way for the last weekly slot.

The run is started with the extra argument `window_days`; the `run` command uses it to widen its
window (one window, one message).
