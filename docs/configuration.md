# Configuration

Kallimachos reads two YAML files from the data folder:

| File | Written by | Holds |
|---|---|---|
| `config.yaml` | `kallimachos init` (wizard), then you | sources, scoring, notification channels, archive, processing, notes target, schedule, hooks. Secrets only as `keyring:kallimachos/<name>` or `env:VAR` references in `*_ref` keys. |
| `profile.yaml` | `kallimachos init` / `profile build`, then you | channels and terms, journals, authors, seeds, exclusions; `status: draft` until `profile approve` |

Data folder: `--data-dir` > `KALLIMACHOS_HOME` > OS default (`~/Library/Application Support/kallimachos`
on macOS, `$XDG_DATA_HOME/kallimachos` or `~/.local/share/kallimachos` on Linux,
`%LOCALAPPDATA%\kallimachos` on Windows). Nothing is written inside the repository.

A complete, commented example is `config.example.yaml` in the repository; the JSON Schema is
`schemas/config.schema.json` and `kallimachos config docs` prints the table of every key with
its type and default. Unknown keys are errors, not warnings (`config validate` exits 2), so a
typo never silently does nothing.

## Everyday commands

```sh
kallimachos config validate          # strict check; exit 2 lists every problem
kallimachos config show              # effective configuration (references, never secret values)
kallimachos config migrate           # upgrade schema_version; keeps config.yaml.bak-v<old>
kallimachos profile show             # the profile table
kallimachos profile approve          # after the user agrees; scans need an approved profile
```

`profile approve` and `notify confirm-email` change one value in place: your comments and layout
in `profile.yaml` / `config.yaml` stay, and a copy `<file>.bak-<timestamp>` is written first.
The global flags (`--json`, `--dry-run`, `--yes`, `--data-dir`) are accepted before or after the
command name (`kallimachos archive run --dry-run`).

## Secrets

Store the value once, outside every file:

```sh
keyring set kallimachos telegram          # then: telegram.bot_token_ref: keyring:kallimachos/telegram
export OPENALEX_API_KEY=...               # then: openalex.api_key_ref: env:OPENALEX_API_KEY
```

`config validate` rejects token-like strings outside `*_ref` keys; `doctor` reports only whether
each reference resolves (present/absent).

## Sections owned by the scheduling and hooks modules

```yaml
schedule:
  backend: local        # local | hermes | github | none
  daily_at: "07:30"
  weekly_on: sun
  weekly_at: "09:30"
  archive_at: "06:30"
  catch_up_missed: true
  max_catch_up_days: 3

hooks:
  session_start: true   # SessionStart summary (when installed with `hooks install`)
  guard: true           # PreToolUse guard
  summary_max_chars: 600
```

See [schedule.md](schedule.md) and [hooks.md](hooks.md). The other sections are described in
[sources.md](sources.md), [processing.md](processing.md), [avenox.md](avenox.md) and
[bridge.md](bridge.md).

## Model layer: what `claude -p` returns (checked 2026-10-18, Claude Code 2.1.301)

One real call, `claude -p "Reply with ok set to true." --output-format json --json-schema
'{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"],"additionalProperties":false}'
--tools "" --no-session-persistence --model haiku`, printed one JSON object with exit code 0:

- the validated object is in **`structured_output`** (`{"ok": true}`);
- `result` holds the same answer as a JSON **string** (`"{\"ok\":true}"`);
- `is_error: false`, `subtype: "success"`, `terminal_reason: "completed"`; `num_turns` was 2 and
  `stop_reason` `tool_use` (which suggests an internal tool call; not documented, not relied on).

On a failure (an exhausted usage limit was observed) the exit code is 1, `is_error` is `true`
and `terminal_reason` is `api_error`, **while `subtype` still says `"success"`**; there is no
`structured_output` and `result` carries the error text. So the provider must test `is_error`
(and the exit code), never `subtype`, and prefer `structured_output` over parsing `result`;
`model/claude_cli.py` already does both.
