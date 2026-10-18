"""http.budget: OpenAlex daily budget and its HTTP integration."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from kallimachos.http.budget import Budget
from kallimachos.http.client import HostPolicy, Http
from kallimachos.http.ua import user_agent
from kallimachos.state.store import read_jsonl
from tests.unit.fakes import FakeClock, ScriptedTransport, response

OA = "https://api.openalex.org/works"
COSTS = {"list": 0.0001, "search": 0.001, "single": 0.0}


class Now:
    def __init__(self, moment: dt.datetime) -> None:
        self.moment = moment

    def __call__(self) -> dt.datetime:
        return self.moment


def make_budget(tmp_path: Path, now: Now, *, daily: float = 0.20) -> Budget:
    return Budget(
        tmp_path / "budget" / "openalex.jsonl",
        daily_budget_usd=daily,
        weekly_extra_usd=0.30,
        daily_cap_usd=0.90,
        unit_cost_usd=COSTS,
        now=now,
    )


def test_target_daily_and_weekly(tmp_path: Path) -> None:
    b = make_budget(tmp_path, Now(dt.datetime(2026, 10, 18, tzinfo=dt.UTC)))
    assert b.target_for(weekly=False) == pytest.approx(0.20)
    assert b.target_for(weekly=True) == pytest.approx(0.50)
    capped = Budget(
        tmp_path / "x.jsonl",
        daily_budget_usd=0.8,
        weekly_extra_usd=0.3,
        daily_cap_usd=0.9,
        unit_cost_usd=COSTS,
    )
    assert capped.target_for(weekly=True) == pytest.approx(0.9)


def test_ledger_exhaustion_and_utc_day_reset(tmp_path: Path) -> None:
    now = Now(dt.datetime(2026, 10, 18, 23, 0, tzinfo=dt.UTC))
    b = make_budget(tmp_path, now, daily=0.003)
    for _ in range(3):
        assert b.can_spend("search")
        b.record("search", {})
    assert not b.can_spend("search")
    assert b.can_spend("single")  # free calls are always allowed while the header allows
    assert b.status().exhausted
    now.moment = dt.datetime(2026, 10, 19, 0, 5, tzinfo=dt.UTC)
    assert b.can_spend("search")
    assert b.status().spent_usd == 0


def test_exhaustion_from_fake_headers(tmp_path: Path) -> None:
    b = make_budget(tmp_path, Now(dt.datetime(2026, 10, 18, tzinfo=dt.UTC)))
    b.record(
        "list",
        {
            "X-RateLimit-Limit-USD": "0.10",
            "X-RateLimit-Remaining-USD": "0.00005",
            "X-RateLimit-Remaining": "999",
            "X-RateLimit-Credits-Used": "1",
            "X-RateLimit-Reset": "3600",
            "X-RateLimit-Something-New": "abc",
        },
    )
    assert not b.can_spend("list")  # header says less than one call is left
    st = b.status()
    assert st.remaining_header_usd == pytest.approx(0.00005)
    assert st.spent_usd == pytest.approx(0.09995)
    line = next(iter(read_jsonl(b.ledger_path)))
    assert line["rate_headers"] == {"x-ratelimit-something-new": "abc"}
    assert line["reset"] == 3600
    assert line["remaining_credits"] == 999


LIVE_HEADERS = {
    # as observed on a live OpenAlex answer (tests/fixtures/openalex/works_list.json)
    "X-RateLimit-Cost-USD": "0.0001",
    "X-RateLimit-Credits-Used": "1",
    "X-RateLimit-Limit": "1000",
    "X-RateLimit-Limit-USD": "0.1",
    "X-RateLimit-Remaining": "999",
    "X-RateLimit-Remaining-USD": "0.0999",
    "X-RateLimit-Reset": "49142",
}


def test_live_headers_remaining_is_credits_not_usd(tmp_path: Path) -> None:
    """``X-RateLimit-Remaining`` (999 credits) must never be read as 999 dollars; the USD view
    comes from the ``*-USD`` headers and the per-call cost from ``X-RateLimit-Cost-USD``."""
    b = make_budget(tmp_path, Now(dt.datetime(2026, 10, 18, tzinfo=dt.UTC)))
    b.record("search", LIVE_HEADERS)  # unit cost of "search" is 0.001; the header says 0.0001
    st = b.status()
    assert st.remaining_header_usd == pytest.approx(0.0999)
    assert st.spent_usd == pytest.approx(0.0001)
    assert not st.exhausted
    assert b.can_spend("search")
    assert Budget.remaining_usd_from(LIVE_HEADERS) == pytest.approx(0.0999)
    assert not Budget.exhausted_from(LIVE_HEADERS)
    assert Budget.exhausted_from({"X-RateLimit-Remaining": "0"})
    assert Budget.exhausted_from({"X-RateLimit-Remaining-USD": "0.0"})
    # without the USD cost header the body's meta.cost_usd is used, then the unit cost
    b.record("list", {"X-RateLimit-Remaining": "998"}, meta_cost_usd=0.00025)
    b.record("list", {})
    assert b.status().spent_usd == pytest.approx(0.0001 + 0.00025 + 0.0001)
    assert Budget.meta_cost_usd(b'{"meta": {"cost_usd": 0.002}}') == pytest.approx(0.002)
    assert Budget.meta_cost_usd(b"<xml/>") is None


def test_zero_credits_exhausts_even_without_usd_headers(tmp_path: Path) -> None:
    b = make_budget(tmp_path, Now(dt.datetime(2026, 10, 18, tzinfo=dt.UTC)))
    b.record("list", {"X-RateLimit-Remaining": "0", "X-RateLimit-Limit": "1000"})
    assert not b.can_spend("list")
    assert not b.can_spend("single")


def test_boolean_operator_counter() -> None:
    assert Budget.boolean_operator_count("a AND b OR c NOT d") == 3
    assert Budget.boolean_operator_count("android or notation") == 0
    six = " AND ".join(["t"] * 7)
    assert Budget.boolean_operator_count(six) == 6


def make_http(tmp_path: Path, budget: Budget, t: ScriptedTransport, clock: FakeClock) -> Http:
    t.clock = clock
    return Http(
        HostPolicy(min_interval_s={"api.openalex.org": 0.15}),
        None,
        budget,
        user_agent(),
        transport=t,
        clock=clock.monotonic,
        wall_clock=clock.time,
        sleep=clock.sleep,
    )


def test_http_refuses_when_budget_exhausted(tmp_path: Path) -> None:
    b = make_budget(tmp_path, Now(dt.datetime(2026, 10, 18, tzinfo=dt.UTC)), daily=0.0002)
    clock = FakeClock()
    t = ScriptedTransport(response(200, b"{}"), response(200, b"{}"))
    http = make_http(tmp_path, b, t, clock)
    assert http.get(OA, params={"filter": "x:1"}) is not None
    assert http.get(OA, params={"filter": "x:2"}) is not None
    assert http.get(OA, params={"filter": "x:3"}) is None
    assert len(t.calls) == 2
    assert "budget exhausted" in http.errors[-1].message
    # other hosts are not charged
    t.responses.append(response(200, b"{}"))
    assert http.get("https://api.example.org/x") is not None


def test_http_429_with_zero_remaining_stops(tmp_path: Path) -> None:
    b = make_budget(tmp_path, Now(dt.datetime(2026, 10, 18, tzinfo=dt.UTC)))
    clock = FakeClock()
    t = ScriptedTransport(
        response(429, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "5000"})
    )
    http = make_http(tmp_path, b, t, clock)
    assert http.get(OA) is None
    assert len(t.calls) == 1
    assert not [s for s in clock.sleeps if s > 1]
    assert "budget exhausted" in http.errors[-1].message
    assert not b.can_spend("list")


def test_http_429_waits_for_ratelimit_reset(tmp_path: Path) -> None:
    b = make_budget(tmp_path, Now(dt.datetime(2026, 10, 18, tzinfo=dt.UTC)))
    clock = FakeClock()
    t = ScriptedTransport(
        response(429, headers={"X-RateLimit-Remaining": "0.5", "X-RateLimit-Reset": "12"}),
        response(200, b"{}", headers={"X-RateLimit-Remaining": "0.4999"}),
    )
    http = make_http(tmp_path, b, t, clock)
    assert http.get(OA) is not None
    assert 12 in clock.sleeps


def test_many_boolean_operators_force_one_second_interval(tmp_path: Path) -> None:
    b = make_budget(tmp_path, Now(dt.datetime(2026, 10, 18, tzinfo=dt.UTC)))
    clock = FakeClock()
    t = ScriptedTransport(*[response(200, b"{}") for _ in range(4)])
    http = make_http(tmp_path, b, t, clock)
    heavy = {"search": " OR ".join(f"term{i}" for i in range(7))}
    http.get(OA, params=heavy, kind="search")
    http.get(OA, params={**heavy, "page": "2"}, kind="search")
    assert t.call_times[1] - t.call_times[0] >= 1.0
    light = {"search": "a OR b"}
    http.get(OA, params=light, kind="search")
    http.get(OA, params={**light, "page": "2"}, kind="search")
    assert t.call_times[3] - t.call_times[2] == pytest.approx(0.15)


def test_ledger_records_cost_per_kind(tmp_path: Path) -> None:
    b = make_budget(tmp_path, Now(dt.datetime(2026, 10, 18, tzinfo=dt.UTC)))
    clock = FakeClock()
    t = ScriptedTransport(response(200, b"{}"), response(200, b"{}"))
    http = make_http(tmp_path, b, t, clock)
    http.get(OA, kind="search", params={"search": "x"})
    http.get(OA + "/W1", kind="single")
    lines = list(read_jsonl(b.ledger_path))
    assert [line["cost_usd"] for line in lines] == [0.001, 0.0]
    assert b.status().spent_usd == pytest.approx(0.001)
