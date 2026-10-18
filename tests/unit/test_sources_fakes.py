"""Shared fakes for the source tests. No tests live here.

``FakeHttp`` is the shared :class:`tests.unit.fakes.FixtureHttp` with prefix routes:
recorded fixtures under ``tests/fixtures/<source>/`` answer, an unrecorded URL raises, and a
failure returns ``None`` plus an :class:`HttpError` exactly like the real ``Http``.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from kallimachos.interfaces import HttpResponse, Window, build
from kallimachos.profile.schema import Profile
from tests.unit.fakes import TRANSPORT_FAILURE, FixtureHttp, RecordedRequest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
REPO = Path(__file__).resolve().parents[2]


def fixture(source: str, case: str) -> HttpResponse:
    meta = json.loads((FIXTURES / source / f"{case}.json").read_text(encoding="utf-8"))
    resp = meta["response"]
    body = (FIXTURES / source / resp["body_file"]).read_bytes()
    return HttpResponse(
        url=meta["request"]["url"], status=resp["status"], headers=dict(resp["headers"]), body=body
    )


def fixture_meta(source: str, case: str) -> dict[str, Any]:
    return dict(json.loads((FIXTURES / source / f"{case}.json").read_text(encoding="utf-8")))


@dataclass
class Fail:
    """A failed exchange: ``Http.get`` returns ``None`` and records an error. ``status=None`` is a
    connection failure (``"<host>: <tries> tries failed"``)."""

    status: int | None
    body: bytes = b""


Reply = HttpResponse | Fail
Call = RecordedRequest


class FakeHttp(FixtureHttp):
    """The source tests' view of the shared :class:`tests.unit.fakes.FixtureHttp`: URL-prefix routes
    (``add(prefix, *replies)``), ``Fail`` replies and the ``calls`` log."""

    def __init__(self, *, repeat_last: bool = True) -> None:
        super().__init__(strict=True, repeat_last=repeat_last)

    def add(self, prefix: str, *replies: Reply) -> FakeHttp:  # type: ignore[override]
        responses = [
            HttpResponse(url="", status=r.status or TRANSPORT_FAILURE, headers={}, body=r.body)
            if isinstance(r, Fail)
            else r
            for r in replies
        ]
        self.add_prefix(prefix, *responses)
        return self

    @property
    def calls(self) -> list[RecordedRequest]:
        return self.requests


def window(start: str, end: str) -> Window:
    return Window(
        start=dt.datetime.fromisoformat(start).replace(tzinfo=dt.UTC),
        end=dt.datetime.fromisoformat(end).replace(tzinfo=dt.UTC),
    )


def example_profile(**extra: Any) -> Profile:
    """The fictional example profile plus optional overrides (e.g. OpenAlex ids)."""
    data = yaml.safe_load((REPO / "profiles" / "example-number-theory.yaml").read_text("utf-8"))
    for key, value in extra.items():
        data[key] = value
    return build(Profile, data)


def empty_profile() -> Profile:
    return build(Profile, {"channels": []})
