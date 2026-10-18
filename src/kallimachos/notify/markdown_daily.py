"""Markdown daily file notifier.

Writes ``<notify.markdown.path>/YYYY-MM-DD.md``; only its own marked block is inserted or replaced;
user text outside the markers is never touched. A file that exists but carries no Kallimachos
marker is a user file: writing into it needs approval (``--yes``, exit 5 otherwise).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from kallimachos.config.schema import Config
from kallimachos.interfaces import ApprovalRequired, CheckResult, Message, NotifyResult
from kallimachos.notify.base import NotifierBase
from kallimachos.state import store

BEGIN = "<!-- kallimachos:begin daily run={run_id} -->"
END = "<!-- kallimachos:end -->"
ITEM = "<!-- kallimachos:item run={run_id} id={candidate_id} -->"
MARKER = "<!-- kallimachos:"


class MarkdownDailyNotifier(NotifierBase):
    name = "markdown"

    def __init__(self, config: Config, directory: Path, *, approved: bool = False) -> None:
        self.config = config
        self.directory = directory
        self.approved = approved

    def path_for(self, run_id: str) -> Path:
        return self.directory / f"{run_id[:10]}.md"

    def send(self, message: Message) -> NotifyResult:
        block = getattr(message, "markdown", "") or block_from_message(message)
        path = self.path_for(message.run_id)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if existing.strip() and MARKER not in existing and not self.approved:
            raise ApprovalRequired(
                f"{path} exists and was not written by kallimachos; ask the user, then rerun "
                "with --yes to add the marked block to it"
            )
        updated = upsert_block(existing, message.run_id, block)
        if updated != existing:
            path.parent.mkdir(parents=True, exist_ok=True)
            store.write_text_atomic(path, updated)
        return NotifyResult(channel=self.name, ok=True, message_ref=str(path))

    def test(self) -> CheckResult:
        """The directory exists (or can be created) and is writable; no file is written."""
        directory = self.directory
        probe = directory
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if probe.exists() and os.access(probe, os.W_OK):
            return CheckResult(ok=True, message=f"daily files go to {directory}")
        return CheckResult(ok=False, message=f"{directory} is not writable")


def _block_re(run_id: str) -> re.Pattern[str]:
    return re.compile(re.escape(BEGIN.format(run_id=run_id)) + r".*?" + re.escape(END), re.S)


def upsert_block(existing: str, run_id: str, block: str) -> str:
    """Replace the block for ``run_id`` if present, else append; idempotent. Text outside the
    markers is kept byte for byte."""
    block = block.strip("\n")
    pattern = _block_re(run_id)
    if pattern.search(existing):
        return pattern.sub(lambda _m: block, existing, count=1)
    if not existing.strip():
        return block + "\n"
    return existing.rstrip("\n") + "\n\n" + block + "\n"


def block_from_message(message: Message) -> str:
    """Fallback block for a plain message (e.g. a hand-built one): the text with numbered item
    lines turned into checkboxes followed by their item markers."""
    by_no = {item.no: item.candidate_id for item in message.items}
    lines = [BEGIN.format(run_id=message.run_id)]
    for line in message.text.split("\n"):
        no = line.split(".", 1)[0] if ". " in line else ""
        if no in by_no:
            lines.append(f"- [ ] {line}")
            lines.append("  " + ITEM.format(run_id=message.run_id, candidate_id=by_no.pop(no)))
        else:
            lines.append(line)
    lines.append(END)
    return "\n".join(lines)
