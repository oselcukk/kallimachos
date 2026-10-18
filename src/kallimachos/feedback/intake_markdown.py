"""Markdown daily files: ticked ``- [x] 3. ...`` lines with ``#tag`` or ``-> tag``; the ``<!--
kallimachos:item -->`` comment gives run and id.

Only lines inside Kallimachos blocks are read. Each ticked and tagged item becomes one
``RawFeedback`` whose text is ``"<no> <tag>"`` and whose ``message_ref`` is
``<file>#<run_id>#<candidate_id>#<tag>``; the cursor keeps the refs already returned, so a poll
is idempotent. A ticked box without a tag is ignored (nothing to record).
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Mapping
from typing import Any

from kallimachos.config.schema import Config
from kallimachos.feedback import grammar
from kallimachos.interfaces import FeedbackChannel, RawFeedback
from kallimachos.notify.base import markdown_directory
from kallimachos.paths import DataLayout

_BLOCK_RE = re.compile(
    r"<!-- kallimachos:begin daily run=(\S+) -->(.*?)<!-- kallimachos:end -->", re.S
)
_TICKED_RE = re.compile(r"^\s*[-*] \[[xX]\]\s+(H?\d+)\.\s")
_BOX_RE = re.compile(r"^\s*[-*] \[[ xX]\]\s")
_ITEM_RE = re.compile(r"^\s*<!-- kallimachos:item run=(\S+) id=(\S+) -->\s*$")
"""The item marker is only honoured on a line of its own (as the renderer writes it); a marker
embedded in a summary or title line is text, never a reference."""


def split_ref(ref: str) -> tuple[str, str, str, str] | None:
    """Inverse of the ``message_ref`` format: (file, run_id, candidate_id, tag)."""
    parts = ref.split("#")
    if len(parts) != 4:
        return None
    return parts[0], parts[1], parts[2], parts[3]


class MarkdownIntake:
    """Implements :class:`kallimachos.interfaces.FeedbackIntake`."""

    name = "markdown"

    def __init__(self, config: Config, layout: DataLayout) -> None:
        self.config = config
        self.layout = layout

    def poll(self, cursor: Mapping[str, Any]) -> tuple[list[RawFeedback], dict[str, Any]]:
        seen = {str(x) for x in cursor.get("seen", [])}
        directory = markdown_directory(self.config, self.layout)
        out: list[RawFeedback] = []
        files = sorted(directory.glob("*.md")) if directory.is_dir() else []
        for path in files:
            text = path.read_text(encoding="utf-8")
            at = _dt.datetime.fromtimestamp(path.stat().st_mtime, _dt.UTC)
            for block in _BLOCK_RE.finditer(text):
                block_run = block.group(1)
                lines = block.group(2).split("\n")
                for i, line in enumerate(lines):
                    m = _TICKED_RE.match(line)
                    if not m:
                        continue
                    run_id, cid = block_run, None
                    for nxt in lines[i + 1 :]:
                        if _BOX_RE.match(nxt):
                            break
                        marker = _ITEM_RE.match(nxt)
                        if marker:
                            run_id, cid = marker.group(1), marker.group(2)
                            break
                    tag = grammar.markdown_tag(line)
                    if tag is None or cid is None:
                        continue
                    ref = f"{path.name}#{run_id}#{cid}#{tag.value}"
                    if ref in seen:
                        continue
                    seen.add(ref)
                    out.append(
                        RawFeedback(
                            at=at,
                            channel=FeedbackChannel.MARKDOWN,
                            text=f"{m.group(1)} {tag.value}",
                            run_hint=run_id,
                            message_ref=ref,
                        )
                    )
        return out, {"seen": sorted(seen)}
