"""``process queue add|sync``: Avenox task-create path with the
stub ``beyin.py`` (argv and metadata checked, id never recreated) and the plain queue-file path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kallimachos.interfaces import PartialFailure, UsageError
from kallimachos.process import queue
from kallimachos.state import store
from tests.unit.test_process_notes_support import (
    EID_A,
    argv_log,
    fake_lanes,
    make_config,
    make_layout,
    make_vault,
)

pytestmark = pytest.mark.usefixtures("fake_lanes")
_ = fake_lanes


@pytest.mark.parametrize(
    ("ident", "expected"),
    [
        (EID_A, f"kallimachos-read-{EID_A}"),
        ("arXiv:2510.01234v2", "kallimachos-read-arxiv-2510-01234v2"),
        ("doi:10.0000/Example.1", "kallimachos-read-doi-10-0000-example-1"),
        ("W123456", "kallimachos-read-w123456"),
    ],
)
def test_task_id_for(ident: str, expected: str) -> None:
    tid = queue.task_id_for(ident)
    assert tid == expected and len(tid) <= 100


def test_task_id_for_rejects_empty() -> None:
    with pytest.raises(UsageError):
        queue.task_id_for("::")


def test_queue_add_dedupes_open_entries(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    first = queue.queue_add(
        layout, ident=EID_A, kind="read", title="T", url="https://example.org/a", note=None
    )
    assert first.eid == EID_A and first.candidate_id is None and first.synced is None
    again = queue.queue_add(layout, ident=EID_A, kind="read", title="T", url=None, note=None)
    assert again.at == first.at
    other = queue.queue_add(
        layout, ident="arXiv:2510.01234", kind="ask", title="U", url=None, note="ask the advisor"
    )
    assert other.candidate_id == "arXiv:2510.01234" and other.eid is None
    assert len(list(store.read_jsonl(layout.reading_queue_file))) == 2


def test_queue_sync_avenox_calls_task_create_and_never_recreates(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    layout = make_layout(tmp_path)
    config = make_config(vault)
    queue.queue_add(
        layout,
        ident=EID_A,
        kind="read",
        title="Fictional Sums",
        url="https://example.org/a",
        note="from feedback 2 read",
    )
    queue.queue_add(
        layout, ident="arXiv:2510.01234", kind="ask", title="Ask about this", url=None, note=None
    )
    synced = queue.queue_sync(layout, config)
    assert [e.synced.avenox_task_id for e in synced if e.synced] == [
        f"kallimachos-read-{EID_A}",
        "kallimachos-read-arxiv-2510-01234",
    ]
    calls = argv_log(vault)
    assert len(calls) == 2 and all(c[:3] == ["--json", "task-create", "--file"] for c in calls)
    task_file = vault / "tasks" / f"kallimachos-read-{EID_A}.md"
    assert task_file.is_file()
    text = task_file.read_text(encoding="utf-8")
    assert 'status: "inbox"' in text and 'owner: "Researcher"' in text
    assert 'project: "example-project"' in text and 'visibility: "internal"' in text
    assert f'"candidate_id": "{EID_A}"' in text and '"kind": "read"' in text
    assert "read: Fictional Sums" in text and "from feedback 2 read" in text
    # second sync: nothing pending, no call
    assert queue.queue_sync(layout, config) == []
    assert len(argv_log(vault)) == 2
    # a new entry whose task already exists is adopted, not recreated
    queue.queue_add(layout, ident=EID_A, kind="ask", title="Fictional Sums", url=None, note=None)
    # same ident -> same task id (kind is a fact, not part of the id)
    synced = queue.queue_sync(layout, config)
    assert (
        synced[0].synced is not None
        and synced[0].synced.avenox_task_id == f"kallimachos-read-{EID_A}"
    )
    assert len(argv_log(vault)) == 2  # beyin.py not called again for an existing task file


def test_task_payload_matches_spec_metadata(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    config = make_config(vault)
    layout = make_layout(tmp_path)
    entry = queue.queue_add(
        layout,
        ident="doi:10.0000/example.1",
        kind="read",
        title="T",
        url="https://doi.org/10.0000/example.1",
        note=None,
    )
    payload = queue.task_payload(entry, config)
    assert payload["source"] == "tasks/kallimachos-read-doi-10-0000-example-1.md"
    assert not payload["text"].startswith("---")
    assert payload["metadata"] == {
        "id": "kallimachos-read-doi-10-0000-example-1",
        "title": "T",
        "kind": "task",
        "revision": 1,
        "status": "inbox",
        "owner": "Researcher",
        "visibility": "internal",
        "facts": {
            "candidate_id": "doi:10.0000/example.1",
            "url": "https://doi.org/10.0000/example.1",
            "kind": "read",
        },
        "project": "example-project",
    }
    config.notes.task_owner = None
    with pytest.raises(UsageError, match="task_owner"):
        queue.task_payload(entry, config)


def test_queue_sync_avenox_failure_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = make_vault(tmp_path)
    layout = make_layout(tmp_path)
    config = make_config(vault)
    queue.queue_add(layout, ident=EID_A, kind="read", title="T", url=None, note=None)
    monkeypatch.setattr(
        queue.avenox,
        "task_create",
        lambda info, f: {
            "status": "error",
            "error": "ValueError",
            "message": "explicit task owner required",
        },
    )
    with pytest.raises(PartialFailure, match="owner"):
        queue.queue_sync(layout, config)
    assert all(e.synced is None for e in queue.current_entries(layout).values())


def test_queue_sync_plain_writes_marked_block(tmp_path: Path) -> None:
    vault = make_vault(tmp_path, avenox=False)
    layout = make_layout(tmp_path)
    config = make_config(vault, avenox=False)
    queue.queue_add(
        layout,
        ident=EID_A,
        kind="read",
        title="Fictional Sums",
        url="https://example.org/a",
        note=None,
    )
    synced = queue.queue_sync(layout, config)
    path = vault / "Literature" / "_Reading-Queue.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Reading queue\n")
    assert "<!-- kallimachos:begin reading_queue -->" in text
    line = (
        "- [ ] Fictional Sums — https://example.org/a · read · " + synced[0].at.date().isoformat()
    )
    assert line in text
    assert synced[0].synced is not None
    assert text.split("\n")[synced[0].synced.queue_file_line - 1] == line
    assert argv_log(vault) == []
    # user text outside the block survives; a second item is appended inside the block
    path.write_text(text + "\nMy own notes under the queue.\n", encoding="utf-8")
    queue.queue_add(
        layout, ident="arXiv:2510.01234", kind="ask", title="Second", url=None, note=None
    )
    queue.queue_sync(layout, config)
    text2 = path.read_text(encoding="utf-8")
    assert "My own notes under the queue." in text2 and text2.count("kallimachos:begin") == 1
    assert "- [ ] Second — arXiv:2510.01234 · ask · " in text2
    assert text2.count("- [ ] ") == 2


def test_queue_sync_plain_refuses_foreign_queue_file(tmp_path: Path) -> None:
    vault = make_vault(tmp_path, avenox=False)
    layout = make_layout(tmp_path)
    config = make_config(vault, avenox=False)
    (vault / "Literature" / "_Reading-Queue.md").write_text("# mine\n", encoding="utf-8")
    queue.queue_add(layout, ident=EID_A, kind="read", title="T", url=None, note=None)
    with pytest.raises(UsageError, match="block"):
        queue.queue_sync(layout, config)


def test_reading_queue_lines_validate_against_schema(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    queue.queue_add(layout, ident=EID_A, kind="read", title="T", url=None, note=None)
    raw = json.loads(layout.reading_queue_file.read_text(encoding="utf-8").splitlines()[0])
    assert set(raw) == {"at", "kind", "title", "candidate_id", "eid", "url", "note", "synced"}
