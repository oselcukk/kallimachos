"""Data directory resolution. Nothing is ever written inside the repository.

Resolution order: explicit ``--data-dir`` > ``KALLIMACHOS_HOME`` > OS default:

* macOS   ``~/Library/Application Support/kallimachos``
* Linux   ``$XDG_DATA_HOME/kallimachos`` (default ``~/.local/share/kallimachos``)
* Windows ``%LOCALAPPDATA%\\kallimachos``
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

ENV_HOME = "KALLIMACHOS_HOME"
APP_NAME = "kallimachos"


def default_data_dir(platform: str | None = None, env: Mapping[str, str] | None = None) -> Path:
    """OS default data directory. ``platform`` defaults to ``sys.platform``; ``env`` to
    ``os.environ``."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    home = Path(env.get("HOME") or env.get("USERPROFILE") or Path.home())
    if platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    if platform.startswith("win"):
        local = env.get("LOCALAPPDATA")
        base = Path(local) if local else home / "AppData" / "Local"
        return base / APP_NAME
    xdg = env.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else home / ".local" / "share"
    return base / APP_NAME


def data_dir(
    override: str | os.PathLike[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path:
    """Resolve the data directory. Expands ``~`` and makes the path absolute; does not create it."""
    env = os.environ if env is None else env
    raw = override or env.get(ENV_HOME)
    path = Path(raw).expanduser() if raw else default_data_dir(platform, env)
    return path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()


def tighten_mode(path: Path, mode: int) -> bool:
    """``chmod`` ``path`` to ``mode`` when it is more permissive (group/other bits set); returns
    whether a change was made. Windows (no POSIX modes) and permission errors leave it alone."""
    if sys.platform == "win32":
        return False
    else:
        try:
            current = path.stat().st_mode & 0o777
            if current & ~mode:
                path.chmod(mode)
                return True
        except OSError:
            return False
        return False


def too_open(path: Path, mode: int) -> bool:
    """True when ``path`` has permission bits outside ``mode`` (POSIX only)."""
    if sys.platform == "win32":
        return False
    else:
        try:
            return bool(path.stat().st_mode & 0o777 & ~mode)
        except OSError:
            return False


@dataclass(frozen=True)
class DataLayout:
    """Named locations inside the data directory."""

    root: Path

    @property
    def config_file(self) -> Path:
        return self.root / "config.yaml"

    @property
    def profile_file(self) -> Path:
        return self.root / "profile.yaml"

    @property
    def weights_file(self) -> Path:
        return self.root / "weights.json"

    @property
    def venue_quality_file(self) -> Path:
        """User-supplied venue quality table, optional."""
        return self.root / "data" / "venue_quality.json"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def seen_file(self) -> Path:
        return self.state / "seen.jsonl"

    @property
    def feedback_file(self) -> Path:
        return self.state / "feedback.jsonl"

    @property
    def budget_dir(self) -> Path:
        return self.state / "budget"

    @property
    def telegram_offset_file(self) -> Path:
        return self.state / "telegram_offset.json"

    @property
    def telegram_sent_file(self) -> Path:
        """``message_id -> run_id`` of sent Telegram messages (reply resolution)."""
        return self.state / "telegram_sent.json"

    @property
    def email_test_file(self) -> Path:
        """Record of the last ``notify test --channel email`` (gate of ``notify confirm-email``)."""
        return self.state / "email_test.json"

    def intake_cursor_file(self, channel: str) -> Path:
        """Poll cursor of a feedback channel (``telegram`` keeps its offset file)."""
        if channel == "telegram":
            return self.telegram_offset_file
        return self.state / f"intake_{channel}.json"

    @property
    def locks(self) -> Path:
        return self.state / "locks"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def http_cache(self) -> Path:
        return self.cache / "http"

    @property
    def archive(self) -> Path:
        return self.root / "archive"

    @property
    def catalog_file(self) -> Path:
        return self.archive / "catalog.jsonl"

    @property
    def unreachable_file(self) -> Path:
        return self.archive / "unreachable.jsonl"

    @property
    def processed_file(self) -> Path:
        return self.archive / "processed.jsonl"

    @property
    def manual_file(self) -> Path:
        return self.archive / "manual.txt"

    @property
    def existing_catalog_file(self) -> Path:
        return self.archive / "existing_catalog.jsonl"

    @property
    def archive_log_file(self) -> Path:
        return self.archive / "log.jsonl"

    @property
    def archive_tmp(self) -> Path:
        return self.archive / "tmp"

    @property
    def archive_outgoing(self) -> Path:
        """Server side of the bridge: PDFs waiting for delivery."""
        return self.archive / "outgoing"

    @property
    def archive_delivered(self) -> Path:
        """Server side of the bridge: PDFs the computer confirmed (moved, never deleted)."""
        return self.archive / "delivered"

    @property
    def process(self) -> Path:
        return self.root / "process"

    @property
    def text_dir(self) -> Path:
        return self.process / "text"

    @property
    def cards_dir(self) -> Path:
        return self.process / "cards"

    @property
    def checks_dir(self) -> Path:
        return self.process / "checks"

    @property
    def semantic_dir(self) -> Path:
        return self.process / "semantic"

    @property
    def ocr_dir(self) -> Path:
        return self.process / "ocr"

    @property
    def chunks_dir(self) -> Path:
        return self.process / "chunks"

    @property
    def graph_file(self) -> Path:
        return self.process / "graph.jsonl"

    @property
    def graph_uncertain_file(self) -> Path:
        return self.process / "graph_uncertain.jsonl"

    @property
    def status_file(self) -> Path:
        return self.process / "status.jsonl"

    @property
    def notes_index_file(self) -> Path:
        return self.process / "notes_index.json"

    @property
    def alias_file(self) -> Path:
        return self.process / "alias.json"

    @property
    def reading_queue_file(self) -> Path:
        return self.process / "reading_queue.jsonl"

    @property
    def verified_errors_file(self) -> Path:
        return self.process / "verified_errors.jsonl"

    @property
    def bridge(self) -> Path:
        return self.root / "bridge"

    @property
    def bridge_incoming(self) -> Path:
        return self.bridge / "incoming"

    @property
    def bridge_delivered_file(self) -> Path:
        return self.bridge / "delivered.jsonl"

    @property
    def bridge_inbox(self) -> Path:
        """Server side: catalog rows pushed by the computer."""
        return self.bridge / "inbox"

    @property
    def bridge_outbox_file(self) -> Path:
        """Computer side: catalog rows the server has not seen yet."""
        return self.bridge / "outbox.jsonl"

    @property
    def bridge_outbox_sent(self) -> Path:
        return self.bridge / "outbox-sent"

    @property
    def bridge_state_file(self) -> Path:
        return self.bridge / "state.json"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def log_file(self) -> Path:
        return self.logs / "kallimachos.log"

    @property
    def daily(self) -> Path:
        """Default ``notify.markdown.path``."""
        return self.root / "daily"

    def run_file(self, run_id: str) -> Path:
        return self.runs / f"{run_id}.json"

    def selection_file(self, run_id: str) -> Path:
        return self.runs / f"{run_id}.selection.json"

    def message_file(self, run_id: str) -> Path:
        return self.runs / f"{run_id}.message.txt"

    def lock_file(self, name: str) -> Path:
        return self.locks / f"{name}.lock"

    def ensure_private_dirs(self) -> list[Path]:
        """Create the data directory tree with mode ``0700`` (owner only) and tighten existing
        directories that are more open; the files inside are written ``0600`` by the store. No-op
        for the mode bits on Windows. Returns the directories created."""
        created: list[Path] = []
        for directory in [self.root, *self.required_dirs()]:
            if not directory.is_dir():
                directory.mkdir(parents=True, exist_ok=True)
                created.append(directory)
            tighten_mode(directory, 0o700)
        return created

    def required_dirs(self) -> list[Path]:
        """Directories ``doctor --fix`` / ``init`` create."""
        return [
            self.state,
            self.budget_dir,
            self.locks,
            self.runs,
            self.http_cache,
            self.archive,
            self.text_dir,
            self.cards_dir,
            self.checks_dir,
            self.semantic_dir,
            self.ocr_dir,
            self.chunks_dir,
            self.logs,
        ]
