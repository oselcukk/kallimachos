"""OpenAlex daily budget in USD.

Spend is the minimum of our own ledger (``state/budget/openalex.jsonl``, UTC day) and what the
provider's response headers say. The next request is refused when it would exceed the target.

Live OpenAlex answers (observed, fixture ``tests/fixtures/openalex/works_list.json``) carry
two families of headers:

* ``X-RateLimit-Limit`` / ``X-RateLimit-Remaining`` / ``X-RateLimit-Credits-Used`` count
  **credits** (requests), not money;
* ``X-RateLimit-Limit-USD`` / ``X-RateLimit-Remaining-USD`` / ``X-RateLimit-Cost-USD`` are the
  money view.

The ledger records the real cost of a call: ``X-RateLimit-Cost-USD`` when present, else
``meta.cost_usd`` from the body when the caller passes it, else ``unit_cost_usd[kind]``. A request
is allowed only when *both* views agree: ``ledger + cost <= target`` and ``remaining_usd >= cost``
(when a USD header has been seen today); a credit count of zero also exhausts the budget.
``status().spent_usd`` is the larger, i.e. more conservative, of the ledger sum and
``limit_usd - remaining_usd`` from the headers. Credit counts are never mistaken for dollars.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kallimachos.interfaces import SchemaViolation
from kallimachos.state import store

KNOWN_HEADERS = {
    "x-ratelimit-limit": "limit_credits",
    "x-ratelimit-remaining": "remaining_credits",
    "x-ratelimit-credits-used": "credits_used",
    "x-ratelimit-limit-usd": "limit_usd",
    "x-ratelimit-remaining-usd": "remaining_usd",
    "x-ratelimit-cost-usd": "cost_header_usd",
    "x-ratelimit-reset": "reset",
}
"""Header (lower-case) -> ledger field. Only the ``*_usd`` fields are money."""

_BOOL_OP_RE = re.compile(r"\b(AND|OR|NOT)\b")

BOOLEAN_OPERATOR_SLOW_LIMIT = 5
"""More boolean operators than this in one search forces a 1 s interval."""


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


@dataclass
class BudgetStatus:
    spent_usd: float
    target_usd: float
    remaining_header_usd: float | None
    exhausted: bool


class Budget:
    def __init__(
        self,
        ledger_path: Path,
        *,
        daily_budget_usd: float,
        weekly_extra_usd: float,
        daily_cap_usd: float,
        unit_cost_usd: Mapping[str, float],
        now: Callable[[], _dt.datetime] = _utc_now,
    ) -> None:
        self.ledger_path = ledger_path
        self.daily_budget_usd = daily_budget_usd
        self.weekly_extra_usd = weekly_extra_usd
        self.daily_cap_usd = daily_cap_usd
        self.unit_cost_usd = dict(unit_cost_usd)
        self.now = now

    # -- helpers -------------------------------------------------------------------------------

    def _today(self) -> str:
        return self.now().astimezone(_dt.UTC).date().isoformat()

    def cost_of(self, kind: str) -> float:
        """Unit cost of one call; unknown kinds (``pdf``) cost nothing."""
        return float(self.unit_cost_usd.get(kind, 0.0))

    def _today_lines(self) -> list[dict[str, Any]]:
        today = self._today()
        try:
            return [line for line in store.read_jsonl(self.ledger_path) if line.get("day") == today]
        except SchemaViolation:
            # a corrupt ledger must not let us overspend: treat the day as exhausted
            return [{"day": today, "cost_usd": float("inf")}]

    def _ledger_spent(self, lines: list[dict[str, Any]]) -> float:
        return sum(float(line.get("cost_usd") or 0.0) for line in lines)

    @staticmethod
    def _last_header(lines: list[dict[str, Any]], name: str) -> float | None:
        for line in reversed(lines):
            value = line.get(name)
            if isinstance(value, int | float):
                return float(value)
        return None

    # -- public API ----------------------------------------------------------------------------

    def target_for(self, *, weekly: bool) -> float:
        """Daily target; weekly runs get ``min(daily + weekly_extra, daily_cap)``."""
        if not weekly:
            return min(self.daily_budget_usd, self.daily_cap_usd)
        return min(self.daily_budget_usd + self.weekly_extra_usd, self.daily_cap_usd)

    def can_spend(self, kind: str, *, weekly: bool = False) -> bool:
        cost = self.cost_of(kind)
        lines = self._today_lines()
        remaining = self._last_header(lines, "remaining_usd")
        credits = self._last_header(lines, "remaining_credits")
        if (remaining is not None and remaining <= 0) or (credits is not None and credits <= 0):
            return False
        if cost == 0.0:
            return True
        if remaining is not None and remaining < cost:
            return False
        return self._ledger_spent(lines) + cost <= self.target_for(weekly=weekly) + 1e-12

    def record(
        self, kind: str, headers: Mapping[str, str], *, meta_cost_usd: float | None = None
    ) -> None:
        """Append the cost of one call and the ``X-RateLimit-*`` headers.

        The cost is ``X-RateLimit-Cost-USD`` when the answer carries it, else ``meta_cost_usd``
        (``meta.cost_usd`` of the body, read by the HTTP client), else the configured unit cost.
        Unknown rate-limit headers are kept in ``rate_headers``.
        """
        lower = {k.lower(): v for k, v in headers.items()}
        values = {
            name: value
            for header, name in KNOWN_HEADERS.items()
            if (value := _float(lower.get(header))) is not None
        }
        cost = values.get("cost_header_usd")
        if cost is None and meta_cost_usd is not None:
            cost = float(meta_cost_usd)
        if cost is None:
            cost = self.cost_of(kind)
        line: dict[str, Any] = {
            "at": self.now().astimezone(_dt.UTC).isoformat(),
            "day": self._today(),
            "kind": kind,
            "cost_usd": cost,
        }
        line.update(values)
        rate_headers = {
            k: v
            for k, v in lower.items()
            if k.startswith("x-ratelimit-") and k not in KNOWN_HEADERS
        }
        if rate_headers:
            line["rate_headers"] = rate_headers
        store.append_jsonl(self.ledger_path, line)

    def status(self, *, weekly: bool = False) -> BudgetStatus:
        lines = self._today_lines()
        spent = self._ledger_spent(lines)
        remaining = self._last_header(lines, "remaining_usd")
        limit = self._last_header(lines, "limit_usd")
        if remaining is not None and limit is not None:
            spent = max(spent, limit - remaining)
        target = self.target_for(weekly=weekly)
        smallest = min((c for c in self.unit_cost_usd.values() if c > 0), default=0.0)
        exhausted = (remaining is not None and remaining <= 0) or spent + smallest > target + 1e-12
        return BudgetStatus(
            spent_usd=round(spent, 6),
            target_usd=target,
            remaining_header_usd=remaining,
            exhausted=exhausted,
        )

    def reset_wait_s(self, headers: Mapping[str, str]) -> float | None:
        """Seconds until the provider resets the budget (``X-RateLimit-Reset``), if given."""
        lower = {k.lower(): v for k, v in headers.items()}
        value = _float(lower.get("x-ratelimit-reset"))
        return None if value is None else max(value, 0.0)

    @staticmethod
    def remaining_usd_from(headers: Mapping[str, str]) -> float | None:
        """``X-RateLimit-Remaining-USD`` (money); ``X-RateLimit-Remaining`` is credits, not USD."""
        lower = {k.lower(): v for k, v in headers.items()}
        return _float(lower.get("x-ratelimit-remaining-usd"))

    @staticmethod
    def exhausted_from(headers: Mapping[str, str]) -> bool:
        """True when the provider says nothing is left: zero USD or zero credits remaining."""
        lower = {k.lower(): v for k, v in headers.items()}
        for name in ("x-ratelimit-remaining-usd", "x-ratelimit-remaining"):
            value = _float(lower.get(name))
            if value is not None and value <= 0:
                return True
        return False

    @staticmethod
    def meta_cost_usd(body: bytes) -> float | None:
        """``meta.cost_usd`` of an OpenAlex JSON body, if present (no exception on any input)."""
        if not body or body[:1] not in (b"{", b" "):
            return None
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return None
        meta = data.get("meta") if isinstance(data, dict) else None
        if not isinstance(meta, dict):
            return None
        try:
            return float(meta.get("cost_usd"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    @staticmethod
    def boolean_operator_count(query: str) -> int:
        """Count AND/OR/NOT operators; more than five forces a 1 s interval."""
        return len(_BOOL_OP_RE.findall(query or ""))
