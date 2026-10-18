# Bridge: server to computer delivery (optional module)

Use the bridge when `run`/`archive run` happen on a server (a VPS, a scheduled agent) and the PDFs
must land in an archive folder on your own computer (iCloud Drive, Google Drive, a local disk).
A single-machine installation does not need it: `archive run` then writes straight to
`archive.root` with `status: stored`.

## Permanent rules

- **Pull only.** The computer connects to the server; the server never connects to the computer.
- **SSH keys only.** `BatchMode=yes` is always set, so a password prompt can never appear.
- **No delete, no overwrite.** New files are written with `O_EXCL`; an existing file is never
  replaced. Moves are hard link + unlink (or a verified copy across file systems).
- **Unmarked files are the user's.** The bridge rewrites only files that carry its marker
  (`<!-- kallimachos:bridge -->`), e.g. `_Incoming/_report.md`.
- **Stop on conflict.** A target with different content stops the round with `CONFLICT`.
- **A file counts as delivered only after the sha256 matches** on the computer *and* the server
  re-checks its own copy (`archive confirm --sha`).

## Configuration

```yaml
bridge:
  enabled: true
  mode: pull
  remote: user@server.example        # SSH key auth only
  remote_data_dir: /home/user/.local/share/kallimachos
  local_archive_root: null           # null = archive.root
  ssh_options: ["BatchMode=yes", "ConnectTimeout=20"]
  retry: 1                           # extra attempts to open the SSH connection (fail2ban friendly)
```

One SSH master connection (`ControlMaster=yes`, `ControlPersist=no`) carries every `rsync`/`ssh`
call of a command. Only opening it is retried.

## One round

```
kallimachos bridge pull       # server outgoing/*.pdf + catalog + unreachable -> bridge/incoming/
kallimachos bridge deliver    # verify and place; then process _Incoming/
kallimachos bridge push-ack   # confirm on the server; send rows created on the computer
kallimachos bridge status     # cached, delivered, waiting for ack, outbox, catalog copy state
```

`bridge push` is an alias of `push-ack`. Every command honors the global `--dry-run` and `--json`.

### Server side

`archive run` on the server leaves new PDFs in `archive/outgoing/<folder>/<name>.pdf` with catalog
`status: outgoing`. `archive confirm --sha <sha256>` (called by `push-ack`) re-hashes the server
copy, moves it to `archive/delivered/<folder>/<name>.pdf` without overwriting, sets
`status: stored` and appends a `delivered` event. Confirming twice is harmless.

### deliver

For each pulled PDF the bridge requires: a safe folder (relative, no `..`, no hidden parts, not
`_Incoming`), a safe name (`^[A-Za-z0-9][A-Za-z0-9._-]{0,220}\.pdf$`), no symlink, a catalog row
with `status: outgoing` and the same folder/name, and the same sha256. The target is
`<archive root>/<folder>/<name>`:

| Target | Result |
|---|---|
| missing | `O_EXCL` copy, sha256 re-checked, recorded in `bridge/delivered.jsonl` |
| same sha256 | counted as delivered (nothing written) |
| different content | `CONFLICT`, the round stops, exit 3 |
| cloud placeholder (macOS dataless) | deferred to the next round |
| path through a symlink / outside the root | rejected |

### `_Incoming/`

Drop a PDF you obtained yourself into `<archive root>/_Incoming/`. The bridge extracts ids from the
file name (DOI-like names, arXiv new/old ids, ePrint `YYYY-NNN`) and the first two pages
(`pdftotext` when installed). It is placed only when exactly **one** open unreachable entry matches
and the entry's title is found on the first pages. The new catalog row has `status: stored` and a
`user_supplied` event; it is queued for the server, which closes the unreachable entry. Everything
else stays in `_Incoming/` and is listed in `_Incoming/_report.md` (`AMBIGUOUS`, `NO_IDENTITY`,
`NO_MATCH`, `TITLE_MISMATCH`, `ALREADY_IN_ARCHIVE`, `PENDING_ON_SERVER`, `CONFLICT`, `WAITING`).
Unreachable entries are placed in `_Unsorted/` under the standard file name.

## Catalog copies and `CATALOG_DIVERGED`

The computer keeps a copy of the server catalog in its own data directory. `bridge pull` replaces
it only when **every old row survives** in the server copy (same id, same sha256). Otherwise it
leaves the copy alone, records `CATALOG_DIVERGED` with the missing ids in `bridge/state.json` and
exits 3.

`bridge reconcile` repairs this instead of leaving the copy stale:

- **on the computer** it merges the pulled server copy into the local catalog by id (events are
  unioned, the newer row wins, empty fields are filled from the other row, each changed row gets a
  `merged` event) and queues rows the server lacks in `bridge/outbox.jsonl`;
- **on the server** (run by `push-ack` after it uploads the outbox to `bridge/inbox/`) it merges
  the inbox the same way and closes the unreachable entries resolved by `user_supplied` rows;
  merged inbox files are renamed `*.merged`, never deleted.

After `reconcile` + `push-ack`, the next `pull` refreshes the copy again. Two rows with the same id
but different sha256 are a real conflict: `reconcile` stops without writing anything.

## Files

| Path | Side | Content |
|---|---|---|
| `bridge/incoming/outgoing/**.pdf` | computer | pulled PDFs (cache) |
| `bridge/incoming/catalog.jsonl`, `unreachable.jsonl` | computer | pulled server copies |
| `bridge/delivered.jsonl` | computer | `delivered` / `acked` lines per sha256 |
| `bridge/outbox.jsonl`, `bridge/outbox-sent/` | computer | rows for the server; sent batches |
| `bridge/state.json` | computer | last pull/deliver/reconcile summary |
| `archive/outgoing/`, `archive/delivered/` | server | waiting / confirmed PDFs |
| `bridge/inbox/*.jsonl[.merged]` | server | rows sent by the computer |

## Tests

The tests use `LocalTransport`: a temporary folder plays the server data directory and
`kallimachos` commands run in-process against it. `SshRsyncTransport` is tested through its argv
with a fake runner; no test opens a network connection.
