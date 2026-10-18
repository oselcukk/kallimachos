# Agent hooks (optional)

Two small hooks for Claude Code and Codex. They are offered during installation and installed
only after the user agrees; `doctor` checks that they still work. There is no receipt hook: an
Avenox vault records sessions itself, and a second record would be a duplicate.

| Hook | Event | What it does |
|---|---|---|
| status summary | `SessionStart` (startup, resume, clear, compact) | adds a few factual lines: last scan and its status, papers in the last message, messages without feedback in the last 7 days, reading queue size, an unconfirmed e-mail channel. At most `hooks.summary_max_chars` (default 600). Silent when Kallimachos is not set up or anything fails. |
| guard | `PreToolUse` | denies file-tool changes to Kallimachos state (`state/`, `runs/`, `archive/*.jsonl`, `process/` records, `weights.json`, `cache/`, `bridge/`) and to the managed blocks of notes, and names the command to use instead. `config.yaml`, `profile.yaml`, `archive/manual.txt`, `daily/` and `logs/` stay editable; so does every line of a note outside the markers. |

Managed blocks are the parts Kallimachos writes inside a note:
`<!-- kallimachos:begin full_reading ... -->` ... `<!-- kallimachos:end full_reading -->`, the
`related` block of `process links`, the reading-queue block. The guard compares the blocks before
and after a `Write`, checks whether an `Edit`/`MultiEdit` (Claude Code) or `apply_patch` hunk
(Codex) touches one, and refuses to forge new markers. The daily Markdown file is exempt: ticking
its boxes is how Markdown feedback works. Shell commands are split like a shell would (quotes,
backslash escapes, `~` and `$HOME`, `cd` followed by relative paths) and only their *write
targets* are judged: redirections, the arguments of `rm`, `mv`, `tee`, `sed -i`, `perl -pi`,
PowerShell `Set-Content` ..., the destination of `cp`, and inline interpreter programs that
write (`python -c "open(p, 'w')"`). Paths are resolved through symlinks and compared without
regard to case on macOS and Windows. Reading a state file, or sending it to `/dev/null`, passes.

The guard is a convenience for agents, not a security boundary: it is fail-open (malformed input
or an internal error allows the call), it sees only what the harness hands it, and a shell has
more ways to write a file than any pattern list covers. The real protections are the commands'
own validation and the backups; the guard just stops the common mistakes early.

## Commands

```sh
kallimachos hooks install --target claude --scope user       # prints the entries, exit 5
kallimachos --yes hooks install --target claude --scope user # after the user's yes
kallimachos hooks status
kallimachos --yes hooks uninstall --target claude --scope user
```

| Target | Scope | File |
|---|---|---|
| `claude` | `user` | `~/.claude/settings.json` (every session) |
| `claude` | `project` | `<project>/.claude/settings.json` (shared with the project) |
| `claude` | `local` | `<project>/.claude/settings.local.json` (this project, personal) |
| `codex` | `user` | `~/.codex/hooks.json` |
| `codex` | `project` | `<project>/.codex/hooks.json` (loaded only when the project is trusted) |

`--project DIR` selects the project (default: current folder). Options in `config.yaml`:

```yaml
hooks:
  session_start: true
  guard: true
  summary_max_chars: 600
```

## What install does to the settings file

Only handlers whose command runs `kallimachos ... hooks session-start` or `... hooks guard` are
added or removed. Every other key and hook (an Avenox vault's `SessionStart`/`PostToolUse`
hooks, your own) stays as it was; the file is re-serialised as 2-space JSON, so its layout may
differ while its content does not. When something changes, a copy
`settings.json.bak-kallimachos-<timestamp>` (a new name every time, never overwritten) is written
first; installing twice changes nothing and writes no backup. A `settings.json` that is a symlink
(a dotfiles checkout) is updated through its target, so the link survives. The installation is
recorded in `state/hooks.json`
for `doctor`. Each handler has `timeout: 10` and runs
`<python> -m kallimachos --data-dir <data folder> hooks <name> --harness <target>`.

Added entries (Claude Code):

```json
{
  "SessionStart": [{"matcher": "startup|resume|clear|compact",
                    "hooks": [{"type": "command", "command": "... hooks session-start --harness claude", "timeout": 10}]}],
  "PreToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash|PowerShell",
                  "hooks": [{"type": "command", "command": "... hooks guard --harness claude", "timeout": 10}]}]
}
```

For Codex the guard matcher is `apply_patch|Edit|Write|Bash`.

## Facts this relies on (official documentation, read 2026-10-18)

Claude Code (`code.claude.com/docs/en/hooks`):

- settings files `~/.claude/settings.json`, `.claude/settings.json`, `.claude/settings.local.json`;
  structure event -> matcher groups -> handlers `{"type": "command", "command", "timeout"}`;
- `SessionStart` matchers `startup`, `resume`, `clear`, `compact`, `fork`; stdout or
  `hookSpecificOutput.additionalContext` is added to the context (capped at 10,000 characters);
- `PreToolUse` receives `tool_name` and `tool_input` (`Write`: `file_path`, `content`; `Edit`:
  `file_path`, `old_string`, `new_string`, `replace_all`; `Bash`: `command`); a call is denied with
  `hookSpecificOutput.permissionDecision: "deny"` and `permissionDecisionReason` (or exit 2).

Codex (`developers.openai.com/codex/hooks`):

- `~/.codex/hooks.json` / `<repo>/.codex/hooks.json` (or inline `[hooks]` tables in
  `config.toml`, but not both in one layer; `hooks install` refuses then and asks you to add the
  entries by hand); hooks are on by default (`[features] hooks = false` turns them off);
- new or changed hooks run only after review with `/hooks`; project hooks need a trusted project;
- file edits arrive as `apply_patch` with the patch in `tool_input.command` (`Edit`/`Write`
  matchers alias it); the shell tool is `Bash`;
- the same `hookSpecificOutput` shapes for `SessionStart` context and `PreToolUse` deny.

The guard parses `*** Update File:`, `*** Add File:`, `*** Delete File:` and `*** Move to:`
lines of a patch. If a future Codex version changes the patch format, the guard lets the call
through (fail-open) rather than blocking work.
