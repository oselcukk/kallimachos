# Kallimachos and Avenox (avenoxbeyin V3)

Avenox is the user's memory infrastructure: the index, `sync`, `context`, `task-create`, receipts,
the frontmatter contract and the companion/knowledge layout. Kallimachos is the side that
processes papers: reading, card, verification, literature note, links, cluster summary. When the
notes live in an Avenox vault, Kallimachos writes there **following Avenox's rules**; without
Avenox the same notes go into a plain Markdown/Obsidian folder.

Everything below was read from the vault-local `beyin.py` wrapper, `beyin_v3*.py`, the beyin
skill and the public README of `github.com/avenoxai/avenoxbeyin` (MIT). Kallimachos does not
vendor or import any Avenox code; it calls the user's installed `beyin.py` as a subprocess.

## Detection

`notes.target: avenox` and `notes.vault_path` pointing at a folder that contains both
`beyin.py` and `.beyin-runtime.json` (`process.avenox.detect`). `.beyin-version` supplies the
installed version. `config validate` refuses `target: avenox` without `beyin.py`;
`doctor` reports detection and the literature-folder index check below.

## How Kallimachos calls beyin.py

`python3 beyin.py --json <command> ...` (Windows: `py -3`), with the vault root as working
directory. The wrapper injects `--vault`/`--state` itself; `--json` forces one JSON object on
stdout. Failures print `{"error": <type>, "message": <text>}` on stderr with exit 1;
`process.avenox.run` turns that into `{"status": "error", ...}` so callers choose the exit code.

| Call | When | Result used |
|---|---|---|
| `sync` | after every note or cluster-summary write | `status`: `succeeded`, `degraded` (warnings), `conflict` -> `note-write` exits 3 after writing the note |
| `task-create --file JSON` | `process queue sync` for each pending `read`/`ask` item | `id`, `status`, `owner`, `source` read back and compared with the request |
| `receipt ...` | **never by the CLI**; the lead agent runs it once at session end | the command line comes from `process.avenox.receipt_command` |

Kallimachos never calls `note-create`, `ingest`, `task-update` or `context`. When an agent looks a
literature note up by hand, `beyin.py context "<eid>"` abstains on a bare eid (observed); search
with words of the title instead, or use `kallimachos process notes-index find <Surname> [year]`.

## Four integration questions, answered from the code

| Question | Avenox code | Kallimachos path |
|---|---|---|
| (a) Does `note-create` accept an arbitrary user folder as `source`? | No: `source` must start with `notes/` or `knowledge/` and end in `.md`; sensitive or excluded directories are refused; an existing file is refused; `text` may not carry frontmatter; `kind: task\|receipt` and `generated` are refused. | Not an obstacle. The beyin skill allows writing with normal file tools when the user keeps another folder layout, and notes without `kind: fact` are indexed too. Literature notes are written with file tools into `<vault>/<literature_folder>/` (default `Literature/`; `notes/` is Avenox's area and is not used), then `beyin.py sync`. One path for every installation. |
| (b) Is there a command to update an existing note? | No (`task-update` exists for tasks only). | Managed blocks: `<!-- kallimachos:begin <kind> ... --> ... <!-- kallimachos:end <kind> -->` for `full_reading` (`note-write --existing`), `related_notes` (`links --apply`) and `reading_queue` (`_Reading-Queue.md`). Frontmatter: only `eid, channel, reader, read_at, source_pdf, zotero, citekey, kallimachos, updated_at` are added or replaced; every other key and all user text stay byte-identical. Then `sync`. |
| (c) What is the `ingest` schema? | `{id, text, source}` where `source` is an existing vault-relative file; it indexes, it does not write files. | Not needed: `sync` indexes the files Kallimachos wrote. |
| (d) Conflicts and concurrency | SQLite `BEGIN IMMEDIATE`, 10 s busy timeout, intent journal with sha read-back; no Markdown file lock; optional `preferences --parallel-sessions on`. | The note file is re-read before the atomic write (`tmp` + `os.replace`); if it changed meanwhile nothing is written (exit 3). `sync` afterwards; `conflict` -> exit 3 and the user's agent resolves it with `beyin.py sync`. `links --apply` and `zotero link` run at chunk closure; an active chunk with eids below `note_written` makes `links --apply` exit 5 (`--force` overrides). |

## Frontmatter Avenox accepts (`note/1`)

Avenox's record validation (`_validate`) type-checks only `kind`, `project`, `status`,
`updated_at` (strings), `visibility` (`public|internal|private`), `facts` (object) and
`revision`; every other key is free metadata. `updated_at` drives recency, so Kallimachos writes it
on every write. `kind` must not be `task`, `receipt` or `inference` for a note.

```yaml
---
kind: note              # notes.frontmatter.kind
project: example-field  # notes.frontmatter.project, else profile.field
visibility: internal    # notes.frontmatter.visibility; private = never in Avenox context
updated_at: 2026-01-03
eid: 0123456789ab
channel: EX
reader: example-reader
read_at: 2026-01-03
source_pdf: Example-Channel/01-Topic/Example_2021_Title.pdf
zotero: zotero://select/library/items/ABCD1234   # optional, process zotero link
citekey: example2021title                        # optional
kallimachos: note/1
---
```

`notes.frontmatter.extra` adds user keys; it can never override a `note/1` key. The cluster
summary uses `cluster/1`: `kind, project, visibility, updated_at, cluster, work_count,
kallimachos`.

## Literature folder and the index

Avenox indexes every Markdown file except directories listed in `<state>/index.json`
(`excluded_dirs`, bare names) and directories whose name carries a sensitive word
(`private`, `secret`, `password`, `credential` and the Turkish `kasa`, `sifre`, `parola`,
`kimlik`, `finans`, `musteri`, `vergi`, `fatura`, `maas`, `ozel`, `gizli` families, folded
case- and accent-insensitively). `process.avenox.literature_folder_indexed` checks the configured
folder against both lists; `doctor` reports a folder that would stay invisible to the index.

## Reading queue as Avenox tasks

`process queue sync` writes one task per pending item with `beyin.py task-create --file`:

```json
{"source": "tasks/kallimachos-read-<slug>.md",
 "text": "read: <title>\n<url>\n<note>",
 "metadata": {"id": "kallimachos-read-<slug>", "title": "<title>", "kind": "task",
              "revision": 1, "status": "inbox", "owner": "<notes.task_owner>",
              "project": "<notes.frontmatter.project>", "visibility": "internal",
              "facts": {"candidate_id": "<eid or candidate id>", "url": "<url>",
                        "kind": "read|ask"}}}
```

The id is derived from the eid or candidate id (lower-case, `[a-z0-9_-]`, at most 100
characters). An existing `tasks/<id>.md` is adopted, never recreated; Avenox's own "task source
or id exists" refusal is treated the same way. `owner` must be explicit (`notes.task_owner`, asked
once during `init`); without it the command exits 2 instead of inventing an owner.

## The single receipt

Avenox wants one source-linked receipt per meaningful piece of work, sent by the agent that did
the work. Kallimachos therefore has **no receipt command**: the process skill tells the lead agent
to run, at session end,

```
python3 beyin.py receipt --harness <client> --event-id <stable id> \
  --summary-file <summary> --ref <note path> [--ref ...] [--session <hook session id>]
```

with the summary produced by `kallimachos process status --summary` (notes written, check and
semantic results, open steps, and the lesson line the beyin skill requires; the process skill
gives its exact spelling). `process.avenox.receipt_command` builds this
line; sub-agents never send receipts. Refs are vault-relative paths of files that exist.

## What Kallimachos never does in a vault

Write into companion, knowledge, receipts, `tasks/` (except through `task-create`) or any folder
other than the literature folder; delete or move files; overwrite a note (same name without
`--existing` is exit 5 with the candidate path); change frontmatter keys outside the list above;
read the whole vault (the agent reads `AGENTS.md`, the beyin skill and the notes it links to).

## Plain folder (no Avenox)

Same files, same frontmatter (harmless), `link_style: markdown` by default, `_Reading-Queue.md`
with a marked block instead of tasks, no `sync`, no receipt. Switching a plain folder to Avenox
later needs only `notes.target: avenox` and one `beyin.py sync`.
