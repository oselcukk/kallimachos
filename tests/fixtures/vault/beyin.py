#!/usr/bin/env python3
"""Stub of the Avenox vault-local ``beyin.py`` for Kallimachos tests (fictional vault).

Behaviour mirrors the documented contract only:

* argv is appended as one JSON line to ``.fake-beyin-argv.jsonl`` in the vault;
* ``sync`` prints ``{"status": "succeeded", ...}``; a ``.fake-sync-conflict`` file in the vault
  makes it ``conflict``;
* ``task-create --file F`` validates the documented payload, refuses an existing ``tasks/<id>.md``
  or id with the real error message, else writes the task file and prints the record;
* ``receipt`` prints ``{"status": "recorded"}``;
* errors are one JSON object on stderr and exit 1, like Avenox.
"""

import json
import re
import sys
from pathlib import Path

VAULT = Path(__file__).resolve().parent
TASK_STATUSES = ("inbox", "active", "waiting", "blocked", "done", "cancelled")


def fail(message, kind="ValueError"):
    print(json.dumps({"error": kind, "message": message}), file=sys.stderr)
    return 1


def main(argv):
    with (VAULT / ".fake-beyin-argv.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps(argv) + "\n")
    args = [a for a in argv if a not in ("--json", "--human")]
    command = args[0] if args else "doctor"
    if command == "sync":
        conflict = (VAULT / ".fake-sync-conflict").exists()
        result = {
            "status": "conflict" if conflict else "succeeded",
            "indexed": 3,
            "deleted": 0,
            "warnings": [],
            "conflicts": [{"source": "Literature/x.md", "reason": "fake"}] if conflict else [],
        }
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    if command == "task-create":
        if len(args) < 3 or args[1] != "--file":
            return fail("task-create needs --file")
        payload = json.loads(Path(args[2]).read_text(encoding="utf-8"))
        source, text, metadata = payload.get("source"), payload.get("text"), payload.get("metadata")
        if (
            not isinstance(source, str)
            or not source.startswith("tasks/")
            or not source.endswith(".md")
        ):
            return fail("task-create source must be a tasks/ Markdown path")
        if not isinstance(text, str) or not text.strip() or not isinstance(metadata, dict):
            return fail("task body and metadata required")
        if text.lstrip().startswith("---"):
            return fail("text must be body only; put frontmatter fields in metadata")
        allowed = {
            "id",
            "title",
            "kind",
            "revision",
            "status",
            "owner",
            "project",
            "visibility",
            "facts",
            "next_action",
            "priority",
            "due_at",
            "updated_at",
            "created_at",
            "completion_contract",
            "completion_criterion",
            "evidence_refs",
        }
        if set(metadata) - allowed:
            return fail("unsupported task metadata")
        if not isinstance(metadata.get("id"), str) or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}", metadata["id"]
        ):
            return fail("stable task id required")
        if not isinstance(metadata.get("owner"), str) or not metadata["owner"].strip():
            return fail("explicit task owner required")
        if metadata.get("status") not in TASK_STATUSES:
            return fail("valid explicit task status required")
        if metadata.get("revision", 1) != 1:
            return fail("new task revision must be 1")
        if metadata.get("kind", "task") != "task":
            return fail("task kind required")
        path = VAULT / source
        if path.exists():
            return fail("task source or id exists; use task-update")
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = (
            ["---"]
            + [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in metadata.items()]
            + ["---", text, ""]
        )
        path.write_text("\n".join(lines), encoding="utf-8")
        record = dict(metadata, source=source, text=text, kind="task", revision=1)
        record["source_sync"] = {"status": "succeeded", "warnings": [], "conflicts": []}
        print(json.dumps(record, ensure_ascii=True, indent=2))
        return 0
    if command == "receipt":
        print(
            json.dumps(
                {
                    "status": "recorded",
                    "event_id": args[args.index("--event-id") + 1]
                    if "--event-id" in args
                    else None,
                }
            )
        )
        return 0
    if command == "doctor":
        print(json.dumps({"status": "observed_metadata"}))
        return 0
    return fail(f"unknown command {command}", "UsageError")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
