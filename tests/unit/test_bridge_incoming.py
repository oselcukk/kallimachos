"""``_Incoming/``: user-dropped PDFs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kallimachos.bridge import deliver as bridge
from kallimachos.bridge.files import MARKER
from kallimachos.bridge.incoming import (
    identities_from_filename,
    identities_from_text,
    keys_intersect,
    title_matches,
)
from kallimachos.interfaces import CatalogEventType, CatalogStatus, UnreachableEntry
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

TITLE = "On the Distribution of Example Primes in Short Intervals"
FIRST_PAGE = f"Journal of Examples\n{TITLE.upper()}\nEXAMPLE AUTHOR A\nAbstract. We study ..."


def _unreachable(
    uid: str = "doi:10.0000/example.1", keys: list[str] | None = None
) -> UnreachableEntry:
    return UnreachableEntry(
        id=uid,
        keys=keys or ["doi:10.0000/example.1"],
        title=TITLE,
        authors=["Example Author A"],
        year=2025,
        reason="paywall",
    )


def _namer(entry: UnreachableEntry) -> tuple[str, str]:
    return "Number-Theory", "Author-A_2025_Distribution-of-Example-Primes.pdf"


def _drop(w: World, name: str, data: bytes) -> Path:
    inc = w.root / "_Incoming"
    inc.mkdir(exist_ok=True)
    path = inc / name
    path.write_bytes(data)
    return path


def _run(w: World, text: str = FIRST_PAGE, **kw: object) -> bridge.DeliverReport:
    bridge.pull(w.transport, w.computer, w.config)
    return bridge.process_incoming(
        w.computer,
        w.config,
        read_text=lambda _p: text,
        namer=_namer,
        **kw,  # type: ignore[arg-type]
    )


def test_single_match_with_title_is_placed_and_reaches_the_server(world: World) -> None:
    world.add_unreachable(_unreachable())
    data = pdf_bytes("user supplied")
    _drop(world, "10.0000_example.1.pdf", data)

    report = _run(world)
    target = world.root / "Number-Theory" / "Author-A_2025_Distribution-of-Example-Primes.pdf"
    assert report.delivered == [target]
    assert target.read_bytes() == data
    assert not (world.root / "_Incoming" / "10.0000_example.1.pdf").exists()

    (row,) = read_rows(world.computer.catalog_file)
    assert row.id == sha(data)[:12] and row.status is CatalogStatus.STORED
    assert row.events[0].type is CatalogEventType.USER_SUPPLIED
    assert row.events[0].detail["unreachable_id"] == "doi:10.0000/example.1"
    report_md = (world.root / "_Incoming" / "_report.md").read_text(encoding="utf-8")
    assert report_md.startswith(MARKER) and "**PLACED**" in report_md

    # push-ack hands the row to the server, which closes the unreachable entry
    bridge.push_ack(world.transport, world.computer, world.config)
    server_rows = {e.id: e for e in read_rows(world.server.catalog_file)}
    assert row.id in server_rows
    (closed,) = [
        json.loads(line)
        for line in world.server.unreachable_file.read_text(encoding="utf-8").splitlines()
    ]
    assert closed["status"] == "closed" and closed["closed_by"] == f"user_supplied:{row.id}"

    # and the next pull does not diverge
    bridge.pull(world.transport, world.computer, world.config)
    assert bridge.read_state(world.computer)["catalog"] != "diverged"


def test_ambiguous_match_goes_to_report(world: World) -> None:
    world.add_unreachable(_unreachable("doi:10.0000/example.1"))
    world.add_unreachable(_unreachable("doi:10.0000/example.2", ["doi:10.0000/example.2"]))
    text = FIRST_PAGE + "\nSee doi:10.0000/example.1 and doi:10.0000/example.2"
    path = _drop(world, "scan.pdf", pdf_bytes("x"))

    report = _run(world, text=text)
    assert report.delivered == []
    assert path.exists()
    assert "**AMBIGUOUS** `scan.pdf`" in (path.parent / "_report.md").read_text(encoding="utf-8")


def test_title_must_be_verified(world: World) -> None:
    world.add_unreachable(_unreachable())
    path = _drop(world, "10.0000_example.1.pdf", pdf_bytes("x"))
    report = _run(world, text="Some unrelated paper about lattices")
    assert report.delivered == [] and path.exists()
    assert any("TITLE_MISMATCH" in n for n in report.notes)


def test_no_identity_and_no_match_stay_in_place(world: World) -> None:
    world.add_unreachable(_unreachable())
    a = _drop(world, "download.pdf", pdf_bytes("a"))
    b = _drop(world, "10.0000_other.9.pdf", pdf_bytes("b"))
    report = _run(world, text="no identifiers here")
    assert a.exists() and b.exists()
    notes = "\n".join(report.notes)
    assert "NO_IDENTITY: download.pdf" in notes and "NO_MATCH: 10.0000_other.9.pdf" in notes


def test_existing_target_is_never_overwritten(world: World) -> None:
    world.add_unreachable(_unreachable())
    target = world.root / "Number-Theory" / "Author-A_2025_Distribution-of-Example-Primes.pdf"
    target.parent.mkdir()
    target.write_bytes(b"%PDF the user's file")
    path = _drop(world, "10.0000_example.1.pdf", pdf_bytes("new"))

    report = _run(world)
    assert report.conflicts and path.exists()
    assert target.read_bytes() == b"%PDF the user's file"


def test_unmarked_report_file_is_left_alone(world: World) -> None:
    world.add_unreachable(_unreachable())
    report_md = world.root / "_Incoming" / "_report.md"
    _drop(world, "download.pdf", pdf_bytes("a"))
    report_md.write_text("my own notes", encoding="utf-8")
    report = _run(world, text="nothing")
    assert report_md.read_text(encoding="utf-8") == "my own notes"
    assert any("not ours" in n for n in report.notes)


def test_catalog_match_is_reported_not_placed(world: World) -> None:
    entry = world.outgoing(
        "Algebra", "Author-A_2026_Example.pdf", pdf_bytes("srv"), keys=["arxiv:2601.00001"]
    )
    assert entry.status is CatalogStatus.OUTGOING
    path = _drop(world, "2601.00001v2.pdf", pdf_bytes("user copy"))
    report = _run(world, text="arXiv:2601.00001v2 " + entry.title)
    assert path.exists() and report.delivered == []
    assert any("PENDING_ON_SERVER" in n for n in report.notes)


def test_dry_run_and_cloud_placeholders(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.add_unreachable(_unreachable())
    path = _drop(world, "10.0000_example.1.pdf", pdf_bytes("x"))
    before = snapshot(world.root)
    report = _run(world, dry_run=True)
    assert snapshot(world.root) == before
    assert any("WOULD_PLACE" in n for n in report.notes)

    monkeypatch.setattr(bridge, "is_dataless", lambda p: Path(p) == path)
    report = _run(world)
    assert report.deferred_dataless == [path] and path.exists()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("10.0000_example.1.pdf", {"doi:10.0000/example.1"}),
        ("10.0000@abc@12.pdf", {"doi:10.0000/abc/12"}),
        ("2601.01234v3.pdf", {"arxiv:2601.01234"}),
        ("2601.01234 (1).pdf", {"arxiv:2601.01234"}),
        ("math_0512003v2.pdf", {"arxiv:math/0512003"}),
        ("2007-154.pdf", {"eprint:2007/154"}),
        ("Author_2026_Title.pdf", set()),
    ],
)
def test_identities_from_filename(name: str, expected: set[str]) -> None:
    assert identities_from_filename(name) == expected


def test_identities_from_text() -> None:
    text = "doi: 10.0000/Example.7). Preprint arXiv:2601.01234v2; eprint.iacr.org/2007/154\n"
    text += "https://doi.org/10.48550/arXiv.2602.00002"
    assert identities_from_text(text) == {
        "doi:10.0000/example.7",
        "arxiv:2601.01234",
        "arxiv:2602.00002",
        "eprint:2007/154",
    }
    assert keys_intersect(["arxiv:2601.01234v2"], {"arxiv:2601.01234"})


def test_title_matching_rules() -> None:
    assert title_matches(TITLE, "ON THE DISTRIBUTION OF EXAMPLE PRIMES ...")
    spaced = " ".join("ONTHEDISTRIBUTIONOFEXAMPLEPRIMESINSHORTINTERVALS")
    assert title_matches(TITLE, spaced)
    assert title_matches(TITLE, "distribution example primes short intervals on the in of")
    assert not title_matches(TITLE, "A different paper on example graphs")
    assert not title_matches("", "anything")
