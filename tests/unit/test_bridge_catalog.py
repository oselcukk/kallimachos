"""Catalog copy, CATALOG_DIVERGED and reconcile.

Regression covered here: the old bridge refused to refresh its catalog copy once the computer held
a row the server lacked, and nothing ever reconciled the two, so the copy stayed stale for good.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from kallimachos.bridge import deliver as bridge
from kallimachos.bridge.base import BridgeConflict
from kallimachos.bridge.catalog_copy import merge, update_copy
from kallimachos.interfaces import (
    CatalogEntry,
    CatalogEvent,
    CatalogEventType,
    CatalogStatus,
)
from tests.unit import test_bridge_support as _support
from tests.unit.test_bridge_support import (
    T0,
    World,
    pdf_bytes,
    read_rows,
    snapshot,
    write_rows,
)

fake_store = _support.fake_store
world = _support.world

NOW = dt.datetime(2026, 2, 1, tzinfo=dt.UTC)


def _row(eid: str, *, at: dt.datetime = T0, **kw: object) -> CatalogEntry:
    return CatalogEntry(
        id=eid,
        title=f"Title {eid}",
        sha256=(eid * 6)[:64].ljust(64, "0"),
        events=[CatalogEvent(at=at, type=CatalogEventType.STORED)],
        **kw,  # type: ignore[arg-type]
    )


def test_copy_is_updated_when_all_old_rows_survive(world: World) -> None:
    write_rows(world.computer.catalog_file, [_row("aaaaaaaaaaaa")])
    write_rows(world.server.catalog_file, [_row("aaaaaaaaaaaa"), _row("bbbbbbbbbbbb")])
    bridge.pull(world.transport, world.computer, world.config)
    assert [e.id for e in read_rows(world.computer.catalog_file)] == [
        "aaaaaaaaaaaa",
        "bbbbbbbbbbbb",
    ]
    assert bridge.read_state(world.computer)["catalog"] == "updated"


def test_divergence_is_reported_and_copy_left_alone(world: World) -> None:
    write_rows(world.computer.catalog_file, [_row("aaaaaaaaaaaa"), _row("cccccccccccc")])
    write_rows(world.server.catalog_file, [_row("aaaaaaaaaaaa"), _row("bbbbbbbbbbbb")])
    before = world.computer.catalog_file.read_bytes()

    bridge.pull(world.transport, world.computer, world.config)
    state = bridge.read_state(world.computer)
    assert state["catalog"] == "diverged"
    assert state["catalog_missing_ids"] == ["cccccccccccc"]
    assert world.computer.catalog_file.read_bytes() == before


def test_reconcile_heals_divergence_on_both_sides(world: World) -> None:
    local_only = _row("cccccccccccc", status=CatalogStatus.STORED)
    write_rows(world.computer.catalog_file, [_row("aaaaaaaaaaaa"), local_only])
    write_rows(world.server.catalog_file, [_row("aaaaaaaaaaaa"), _row("bbbbbbbbbbbb")])
    bridge.pull(world.transport, world.computer, world.config)
    assert bridge.read_state(world.computer)["catalog"] == "diverged"

    # computer side: union by id, `merged` event on the row that came from the server
    assert bridge.reconcile(world.computer, world.config) == 1
    local = {e.id: e for e in read_rows(world.computer.catalog_file)}
    assert set(local) == {"aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"}
    assert local["bbbbbbbbbbbb"].events[-1].type is CatalogEventType.MERGED
    assert local["aaaaaaaaaaaa"].events[-1].type is CatalogEventType.STORED  # unchanged row
    assert [e.id for e in read_rows(bridge.outbox_file(world.computer))] == ["cccccccccccc"]

    # push-ack carries the local-only row to the server, which merges it
    bridge.push_ack(world.transport, world.computer, world.config)
    server = {e.id: e for e in read_rows(world.server.catalog_file)}
    assert "cccccccccccc" in server
    assert server["cccccccccccc"].events[-1].type is CatalogEventType.MERGED
    assert not bridge.outbox_file(world.computer).exists()
    assert list((world.server.bridge / "inbox").glob("*.jsonl.merged"))

    # next pull: no divergence any more, the copy is refreshed again
    bridge.pull(world.transport, world.computer, world.config)
    assert bridge.read_state(world.computer)["catalog"] in {"updated", "unchanged"}
    assert {e.id for e in read_rows(world.computer.catalog_file)} == set(server)


def test_reconcile_is_idempotent(world: World) -> None:
    write_rows(world.computer.catalog_file, [_row("aaaaaaaaaaaa")])
    write_rows(world.server.catalog_file, [_row("aaaaaaaaaaaa"), _row("bbbbbbbbbbbb")])
    bridge.pull(world.transport, world.computer, world.config)
    bridge.reconcile(world.computer, world.config)
    first = world.computer.catalog_file.read_bytes()
    assert bridge.reconcile(world.computer, world.config) == 0
    assert world.computer.catalog_file.read_bytes() == first


def test_sha_clash_stops_reconcile_without_writing(world: World) -> None:
    mine = _row("aaaaaaaaaaaa")
    theirs = dataclasses.replace(mine, sha256="f" * 64)
    write_rows(world.computer.catalog_file, [mine])
    write_rows(world.server.catalog_file, [theirs, _row("bbbbbbbbbbbb")])
    bridge.pull(world.transport, world.computer, world.config)
    before = snapshot(world.computer.root)
    with pytest.raises(BridgeConflict):
        bridge.reconcile(world.computer, world.config)
    after = snapshot(world.computer.root)
    assert after["archive/catalog.jsonl"] == before["archive/catalog.jsonl"]


def test_merge_takes_newer_row_and_unions_events() -> None:
    old = _row("aaaaaaaaaaaa", status=CatalogStatus.OUTGOING, folder="Algebra")
    new = dataclasses.replace(
        old,
        status=CatalogStatus.STORED,
        folder=None,
        events=[
            *old.events,
            CatalogEvent(at=T0 + dt.timedelta(days=1), type=CatalogEventType.DELIVERED),
        ],
    )
    res = merge([old], [new], source="server", now=NOW)
    (row,) = res.entries
    assert row.status is CatalogStatus.STORED  # newer side wins
    assert row.folder == "Algebra"  # missing field filled from the other side
    assert [e.type for e in row.events] == [
        CatalogEventType.STORED,
        CatalogEventType.DELIVERED,
        CatalogEventType.MERGED,
    ]
    assert row.events[-1].detail == {"source": "server"}
    assert res.changed == ["aaaaaaaaaaaa"]


def test_update_copy_without_remote_is_a_no_op(world: World) -> None:
    res = update_copy(world.computer.catalog_file, world.tmp / "missing.jsonl")
    assert not res.updated and not res.diverged


def test_local_outgoing_rows_never_count_as_divergence(world: World) -> None:
    entry = world.outgoing("Algebra", "Author-A_2026_Example.pdf", pdf_bytes("a"))
    bridge.pull(world.transport, world.computer, world.config)
    bridge.deliver(world.computer, world.config)
    bridge.push_ack(world.transport, world.computer, world.config)
    bridge.pull(world.transport, world.computer, world.config)
    assert bridge.read_state(world.computer)["catalog"] == "updated"
    (row,) = read_rows(world.computer.catalog_file)
    assert row.id == entry.id and row.status is CatalogStatus.STORED
