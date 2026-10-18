# Processing: from PDF to verified note, links and cluster summary

This document explains the quality protocol behind `kallimachos process` and the files it
produces. The agent-facing procedure is the `process` skill (`.agents/skills/process/SKILL.md`);
the Avenox side is `docs/avenox.md`; the shared shapes are `src/kallimachos/interfaces.py`.

## Why "the quote was found" is not quality

The first version of this pipeline checked that every quotation in a card could be located in the
PDF text. Quotes were found, cards validated, and notes still missed main results, dropped
hypotheses and turned concrete open problems into vague directions. Quote-findability measures
the recorder's copying, not the reader's understanding. The pipeline therefore has two layers:

1. **Mechanical checks** (`process check`, `process cite`): quotes located (declared page, then all
   pages, then neighboring pairs), scope regexes, split numbers, reference-section exclusion,
   false alarms, schema, importance rule. Failure -> `check_failed`, back to the recorder.
2. **Semantic verification** (`process semantic-record`): a *different* agent than the reader
   compares the note body and the card with the text and reports mismatches of four kinds:
   (a) a concrete open problem or solution claim missing, (b) a main result missing, (c) a
   number/page/label mismatch, (d) a lost qualifier or hypothesis. Without a `pass` record
   `note-write` refuses (exit 5). Per chunk a sample (`process.verify_sample_ratio`) is checked
   against page images; findings go to `verified_errors.jsonl`.

Only after both layers does the note reach the user's folder.

## Roles

| Role | Input | Output | Owner of |
|---|---|---|---|
| Lead | chunk list, status table | commands, dispatch, closure, one receipt | `status.jsonl` (through commands) |
| Reader | full text, page images, catalog citation, profile | note body (Markdown, no frontmatter), 4-line report | nothing in the vault directly |
| Recorder | text + note body | card (`card/4`), check and cite runs | `cards/<eid>.json` |
| Verifier | text, note body, card | `semantic_result/1` JSON | `semantic/<eid>.json` |
| Cluster writer / auditor / fixer | notes, list file, graph, card items | `_Cluster-Summary.md`, `_Open-Problems.md` | the two `_` files of the folder |

Readers never write cards, recorders never change notes, verifiers never fix either: every
correction travels back through the lead. Sub-agents never send Avenox receipts.

## Chunk lifecycle

```
chunk create -> extract [ocr] -> card-write -> check -> cite -> semantic-record -> note-write
             -> (every eid note_written) -> links fix-fake --apply -> links --apply
             -> zotero link -> cluster start -> cluster write -> cluster open-problems -> receipt
```

States (`state/machine.py`): `downloaded -> text_extracted -> (ocr_done) -> card_written ->
check_passed | check_failed -> semantic_verified | semantic_failed -> note_written -> linked ->
cluster_summarized`. Gates: `note-write` needs `semantic_verified`; `links --apply` refuses while
an active chunk has eids below `note_written` (exit 5, `--force` with approval); `cluster start`
refuses while any eid of the folder is below `linked` (exit 5). `chunks/<n>/log.md` is produced by
commands, not edited by hand.

## The literature note (`note/1`)

Path: `<vault>/<literature_folder>/<channel.folder>/<subfolder>/<Surname>-<Year>-<Short-Title>.md`.
The subfolder mirrors the first two folders of the archive path; the name is ASCII and hyphens;
1-3 author surnames joined by `-`, four or more as `<First>-<etal>` (`notes.etal_suffix`, `vd`
for Turkish users); the year is the catalog year (`undated` when unknown); the short title keeps
LaTeX words, drops formatting commands and leading articles, at most 7 words / 60 characters.

The body comes from the reader; `note-write` validates it (no frontmatter, one H1, the eleven
section headings in the configured language with "Not found" mandatory, the `[[` rule) and adds
the frontmatter (`docs/avenox.md` lists the field set). `--existing PATH` keeps the user's note
untouched and nests the body, headings demoted one level, inside the managed `full_reading`
block; a previous model's block is replaced in place; only the Kallimachos frontmatter keys are
added or updated. The command is idempotent: the same body twice changes nothing. `PATH` must be
an existing `.md` file inside the literature folder (relative paths count from the vault root),
reached without a symlink component; anything else exits 2.

`notes.personal_relation_section` (default true) adds `## Relation to my work (inference)` at the
end: the reader sees the profile and writes only connections it actually observed, each tied to
the result it rests on and tagged `[inference]`; "no direct relation" is a valid answer. This is
the only place where the note goes beyond the work, and it is marked as such.

## Links

`process cite` writes the citation graph (`graph.jsonl`, uncertain edges in
`graph_uncertain.jsonl`). `process links` reads it with `notes_index.json` and, for every note,
lists the cited works that have a note and are not yet linked anywhere in the user's text, then
writes one idempotent managed block:

```
<!-- kallimachos:begin related_notes -->
## Related notes in the archive (from the citation graph, automatic)
- [[Literature/Example-Channel/01-Topic/Other-2020-Second-Example]] — references [3]
<!-- kallimachos:end related_notes -->
```

Links inside the automatic block do not count as "already linked"; uncertain edges never enter
a note and are listed in `process/links_uncertain.md`. Without `--apply` the command only
reports. `notes.link_style` chooses `[[wikilink]]` (default for Avenox) or a relative Markdown
link (default for plain folders).

`process links fix-fake` repairs what Obsidian would read as a link: `[[...]]` whose target does
not exist and contains a math character (`,;+×−=\^_$′'·()`) or nested brackets -> `[ [ ... ] ]`;
`[x](y)` whose target is not a URL, `mailto:`, `obsidian:`, `#anchor`, a path or a file with an
extension -> `[x] (y)`; an unclosed `[[` on a line -> `[ [`. Fenced and inline code are left
alone. Scope: the literature folder; `--all` (whole vault) needs `--yes` after the user's approval.

## Cluster summary and open problems

`process cluster start FOLDER` returns the skeleton and the input packet (note paths, graph edges
inside the folder, card items). The writer uses notes, list file and graph only (no PDFs); every
bullet links to a note; anything not in the sources is tagged `[comment]`; the intro paragraph
states this method and the graph's limits. Sections: Map of the cluster (main lines; chronology
and influence chain) - Notation glossary - Numerical results - Errors and corrections - Suggested
concept notes. There is no "works closest to the user" section. `cluster write` validates
headings, intro paragraph, tags and the link rule, writes `<FOLDER>/_Cluster-Summary.md` with the
`cluster/1` frontmatter (replacing only its own earlier summary), records `cluster_summarized`
and runs `sync` on Avenox. The audit result goes in through `--verified-by`.

`process cluster open-problems FOLDER` prints the closure table
`# | Work | Problem (author's words, p.) | Concreteness | Status | Evidence` from the cards'
open items (`open_problem`, `question`, `conjecture`, `research_direction`, status `open` /
`partly_solved_here`). A problem that another work in the archive claims to solve (card status
`solved_elsewhere`, or a `claimed_solution` item whose `target` names it in a work that cites the
problem's work) is not proposed as open. The agent fills `Status` and writes
`<FOLDER>/_Open-Problems.md`; no proximity judgments.

## Reading queue

`read`/`ask` feedback becomes `process queue add` entries in `process/reading_queue.jsonl`
(append-only, last line per item wins). `process queue sync` delivers them: Avenox tasks
(`beyin.py task-create`, id `kallimachos-read-<id>`, never recreated) or lines inside the marked
block of `<literature_folder>/_Reading-Queue.md`
(`- [ ] <title> — <url> · read · <date>`). A queue file without the block is a user file and is
not touched (exit 2).

## Files

| File | Written by | Content |
|---|---|---|
| `process/notes_index.json` | `note-write`, `notes-index rebuild` | `{eid: {path, title, updated_at, zotero_key?, citekey?}}` |
| `process/graph.jsonl`, `graph_uncertain.jsonl` | `cite` | `{source_eid, target_eid, key, method, score, text}` |
| `process/links_uncertain.md` | `links --apply` | human-readable list of uncertain edges |
| `process/reading_queue.jsonl` | `queue add/sync` | `reading_queue/1` lines |
| `process/status.jsonl` | every command | `{eid, state, at, by}` |
| `<literature_folder>/.../<note>.md` | `note-write`, `links --apply` | the note; managed blocks |
| `<folder>/_Cluster-Summary.md` | `cluster write` | `cluster/1` summary |
| `<literature_folder>/_Reading-Queue.md` | `queue sync` (plain) | marked block of queue lines |

Every state file is validated against `schemas/` on write and NFC-normalized; Markdown files are
written atomically (`tmp` + `os.replace`).
