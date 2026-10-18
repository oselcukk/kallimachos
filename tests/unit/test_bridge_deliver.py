"""Bridge pull / deliver / push-ack with a local fake server."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kallimachos.bridge import deliver as bridge
from kallimachos.interfaces import CatalogEventType, CatalogStatus, PartialFailure
from tests.unit import test_bridge_support as _support
from tests.unit.test_bridge_support import (
    World,
    pdf_bytes,
    read_rows,
    sha,
    snapshot,
)

fake_store = _support.fake_store
world = _support.world


def _round(w: World) -> bridge.DeliverReport:
    bridge.pull(w.transport, w.computer, w.config)
    return bridge.deliver(w.computer, w.config)


def test_pull_deliver_ack_round_trip(world: World) -> None:
    a = world.outgoing("Algebra", "Author-A_2026_First-Example.pdf", pdf_bytes("a"))
    b = world.outgoing("persons/Example-Person", "Author-B_2026_Second.pdf", pdf_bytes("b"))

    pulled = bridge.pull(world.transport, world.computer, world.config)
    assert [p.name for p in pulled] == sorted([a.filename or "", b.filename or ""])
    assert {e.id for e in read_rows(world.computer.catalog_file)} == {a.id, b.id}

    report = bridge.deliver(world.computer, world.config)
    assert report.conflicts == [] and report.rejected == []
    assert (world.root / "Algebra" / "Author-A_2026_First-Example.pdf").read_bytes() == pdf_bytes(
        "a"
    )
    assert (world.root / "persons/Example-Person/Author-B_2026_Second.pdf").exists()
    assert len(bridge.pending_acks(world.computer)) == 2

    assert bridge.push_ack(world.transport, world.computer, world.config) == 2
    assert bridge.pending_acks(world.computer) == []

    server_rows = {e.id: e for e in read_rows(world.server.catalog_file)}
    for entry in (a, b):
        row = server_rows[entry.id]
        assert row.status is CatalogStatus.STORED
        assert row.events[-1].type is CatalogEventType.DELIVERED
        assert row.events[-1].detail["sha256"] == entry.sha256
        assert not (world.server.archive / "outgoing" / (entry.path or "")).exists()
        assert (world.server.archive / "delivered" / (entry.path or "")).exists()

    # next round: nothing new, the local catalog copy follows the server (stored)
    again = _round(world)
    assert again.delivered == [] and again.conflicts == []
    assert all(e.status is CatalogStatus.STORED for e in read_rows(world.computer.catalog_file))
    assert bridge.read_state(world.computer)["catalog"] == "updated"


def test_sha_mismatch_is_not_delivered(world: World) -> None:
    entry = world.outgoing("Algebra", "Author-A_2026_Example.pdf", pdf_bytes("a"))
    bridge.pull(world.transport, world.computer, world.config)
    cached = bridge.cache_outgoing(world.computer) / "Algebra" / (entry.filename or "")
    cached.write_bytes(pdf_bytes("tampered in transit"))

    report = bridge.deliver(world.computer, world.config)
    assert report.delivered == []
    assert any(r.startswith("SHA_MISMATCH") for r in report.rejected)
    assert not (world.root / "Algebra" / (entry.filename or "")).exists()
    assert bridge.pending_acks(world.computer) == []


def test_existing_target_with_same_sha_counts_as_delivered(world: World) -> None:
    entry = world.outgoing("Algebra", "Author-A_2026_Example.pdf", pdf_bytes("a"))
    target = world.root / "Algebra" / (entry.filename or "")
    target.parent.mkdir()
    target.write_bytes(pdf_bytes("a"))
    before = target.stat().st_mtime_ns

    report = _round(world)
    assert report.already_present == [target] and report.delivered == []
    assert target.stat().st_mtime_ns == before
    assert [r["sha256"] for r in bridge.pending_acks(world.computer)] == [entry.sha256]


def test_conflict_stops_and_never_overwrites(world: World) -> None:
    world.outgoing("Algebra", "Author-A_2026_Example.pdf", pdf_bytes("a"))
    world.outgoing("Algebra", "Author-Z_2026_Later.pdf", pdf_bytes("z"))
    target = world.root / "Algebra" / "Author-A_2026_Example.pdf"
    target.parent.mkdir()
    target.write_bytes(b"%PDF-1.4 the user's own different file")
    before = snapshot(world.root)

    report = _round(world)
    assert len(report.conflicts) == 1 and "CONFLICT" in report.conflicts[0]
    assert report.delivered == []  # stopped: the second file was not placed
    assert snapshot(world.root) == before
    assert bridge.read_state(world.computer)["conflicts"] == report.conflicts


def test_dataless_target_is_deferred(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = world.outgoing("Algebra", "Author-A_2026_Example.pdf", pdf_bytes("a"))
    target = world.root / "Algebra" / (entry.filename or "")
    target.parent.mkdir()
    target.write_bytes(b"")  # a cloud placeholder has no bytes on disk
    monkeypatch.setattr(bridge, "is_dataless", lambda p: Path(p) == target)

    report = _round(world)
    assert report.deferred_dataless == [target]
    assert report.conflicts == [] and report.delivered == []
    assert target.read_bytes() == b""


def test_invalid_files_are_rejected(world: World) -> None:
    good = world.outgoing("Algebra", "Author-A_2026_Example.pdf", pdf_bytes("a"))
    bridge.pull(world.transport, world.computer, world.config)
    cache = bridge.cache_outgoing(world.computer)
    (cache / "Algebra" / "not-in-catalog.pdf").write_bytes(pdf_bytes("x"))
    (cache / "Algebra" / "bad name;rm.pdf").write_bytes(pdf_bytes("y"))
    (cache / ".hidden").mkdir()
    (cache / ".hidden" / "Author_2026_X.pdf").write_bytes(pdf_bytes("h"))
    os.symlink(cache / "Algebra" / (good.filename or ""), cache / "Algebra" / "Link_2026_X.pdf")

    report = bridge.deliver(world.computer, world.config)
    assert [p.name for p in report.delivered] == [good.filename]
    reasons = "\n".join(report.rejected)
    assert "NOT_IN_CATALOG" in reasons
    assert "bad name;rm.pdf" in reasons
    assert ".hidden/Author_2026_X.pdf" in reasons
    assert "Link_2026_X.pdf" in reasons


def test_symlinked_folder_in_archive_is_not_followed(world: World) -> None:
    outside = world.tmp / "outside"
    outside.mkdir()
    os.symlink(outside, world.root / "Algebra")
    world.outgoing("Algebra", "Author-A_2026_Example.pdf", pdf_bytes("a"))

    report = _round(world)
    assert report.delivered == []
    assert any("outside the archive root" in r for r in report.rejected)
    assert list(outside.iterdir()) == []


def test_dry_run_writes_nothing(world: World) -> None:
    world.outgoing("Algebra", "Author-A_2026_Example.pdf", pdf_bytes("a"))
    bridge.pull(world.transport, world.computer, world.config)
    before_root = snapshot(world.root)
    before_home = snapshot(world.computer.root)

    report = bridge.deliver(world.computer, world.config, dry_run=True)
    assert report.notes == ["would deliver: Algebra/Author-A_2026_Example.pdf"]
    assert snapshot(world.root) == before_root
    assert snapshot(world.computer.root) == before_home
    assert bridge.pull(world.transport, world.computer, world.config, dry_run=True) == []


def test_unmarked_user_files_are_untouched(world: World) -> None:
    (world.root / "Algebra").mkdir()
    (world.root / "Algebra" / "my-notes.pdf").write_bytes(b"user data")
    (world.root / "Algebra" / "Reading list.md").write_text("mine", encoding="utf-8")
    before = snapshot(world.root)
    world.outgoing("Algebra", "Author-A_2026_Example.pdf", pdf_bytes("a"))

    _round(world)
    after = snapshot(world.root)
    assert {k: v for k, v in after.items() if k in before} == before
    assert set(after) - set(before) == {"Algebra/Author-A_2026_Example.pdf"}


def test_failed_confirmation_is_kept_for_the_next_round(world: World) -> None:
    entry = world.outgoing("Algebra", "Author-A_2026_Example.pdf", pdf_bytes("a"))
    _round(world)
    # the server copy changed after the pull: the server refuses to confirm
    (world.server.archive / "outgoing" / (entry.path or "")).write_bytes(pdf_bytes("changed"))
    with pytest.raises(PartialFailure):
        bridge.push_ack(world.transport, world.computer, world.config)
    assert [r["sha256"] for r in bridge.pending_acks(world.computer)] == [sha(pdf_bytes("a"))]
    row = next(e for e in read_rows(world.server.catalog_file) if e.id == entry.id)
    assert row.status is CatalogStatus.OUTGOING


def test_confirm_is_idempotent_and_verifies(world: World) -> None:
    entry = world.outgoing("Algebra", "Author-A_2026_Example.pdf", pdf_bytes("a"))
    assert entry.sha256
    assert bridge.confirm(world.server, entry.sha256).startswith("confirmed")
    assert bridge.confirm(world.server, entry.sha256).startswith("already confirmed")
    rows = read_rows(world.server.catalog_file)
    assert sum(ev.type is CatalogEventType.DELIVERED for ev in rows[0].events) == 1


def test_file_moved_by_the_user_is_not_copied_twice(world: World) -> None:
    """A delivery the user moved to another folder (same sha elsewhere under the archive root)
    is reported as MOVED, recorded as delivered at its new place and never copied again."""
    a = world.outgoing("Algebra", "Author-A_2026_First-Example.pdf", pdf_bytes("moved"))
    report = _round(world)
    delivered = world.root / "Algebra" / "Author-A_2026_First-Example.pdf"
    assert report.delivered == [delivered]

    # the user re-files it; delivered.jsonl is lost (e.g. a fresh computer-side data directory)
    new_home = world.root / "Reading-List" / "First-Example.pdf"
    new_home.parent.mkdir()
    delivered.rename(new_home)
    world.computer.bridge_delivered_file.unlink()

    before = snapshot(world.root)
    again = _round(world)
    assert again.delivered == [] and again.conflicts == []
    assert again.moved == [(delivered, new_home)]
    assert any(n.startswith("MOVED: Algebra/") and "Reading-List/" in n for n in again.notes)
    assert snapshot(world.root) == before  # nothing copied, nothing touched
    pending = bridge.pending_acks(world.computer)
    assert [p["sha256"] for p in pending] == [a.sha256]
    assert pending[0]["path"] == "Reading-List/First-Example.pdf"
    # push-ack confirms the server copy as usual
    assert bridge.push_ack(world.transport, world.computer, world.config) == 1
