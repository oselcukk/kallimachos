"""Venue quality: user-provided quartiles or the OpenAlex
open-data indicator.

SJR/Scopus dumps never enter the repository. Layer 1 is the user's own
``data/venue_quality.json`` (``{issn: {quartile_best, quartile_by_year, discontinued, source}}``);
layer 2 is the indicator derived from an OpenAlex ``sources/<id>`` record. Both are annotation
and certain-pass input only; neither drops a candidate by itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kallimachos.config.schema import VenueIndicatorConfig
from kallimachos.interfaces import SchemaViolation, VenueIndicator, build


@dataclass
class VenueQuality:
    """One entry of the user's ``data/venue_quality.json`` (``{issn: {...}}``)."""

    quartile_best: int | None = None
    quartile_by_year: dict[str, int] = field(default_factory=dict)
    discontinued: bool = False
    source: str = ""

    def quartile_for(self, year: int | None) -> int | None:
        """Quartile of ``year`` when listed, else nearest listed year, else ``quartile_best``."""
        if year is not None and self.quartile_by_year:
            if str(year) in self.quartile_by_year:
                return self.quartile_by_year[str(year)]
            years = sorted(int(y) for y in self.quartile_by_year if y.isdigit())
            if years:
                nearest = min(years, key=lambda y: abs(y - year))
                return self.quartile_by_year[str(nearest)]
        return self.quartile_best

    def short(self) -> str:
        if self.discontinued:
            return "discontinued"
        return f"Q{self.quartile_best}" if self.quartile_best else "unranked"


def normalize_issn(issn: str) -> str:
    return issn.strip().upper()


def load_user_quality(path: Path) -> dict[str, VenueQuality]:
    """Read the user's quality file; a missing file is an empty mapping. Keys are normalised
    ISSNs. Shape errors raise :class:`SchemaViolation` (exit 2)."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SchemaViolation(f"{path}: invalid JSON ({exc.msg} at line {exc.lineno})") from exc
    if not isinstance(raw, dict):
        raise SchemaViolation(f"{path}: expected an object keyed by ISSN")
    out: dict[str, VenueQuality] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise SchemaViolation(f"{path}: ISSN keys must be strings")
        try:
            out[normalize_issn(key)] = build(VenueQuality, value, path=f"{key}")
        except (TypeError, ValueError) as exc:
            raise SchemaViolation(f"{path}: {key}: {exc}") from exc
    return out


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def indicator_from_openalex(source: dict[str, Any], cfg: VenueIndicatorConfig) -> VenueIndicator:
    """Fields read optionally: works_count, cited_by_count, summary_stats.2yr_mean_citedness,
    h_index, i10_index, is_in_doaj, is_oa, type; missing -> ``unknown``.

    ``2yr_mean_citedness >= strong_min_citedness`` -> strong;
    ``< weak_max_citedness`` with ``works_count > weak_min_works`` -> weak; a citedness value
    otherwise -> normal; no citedness -> unknown (a large ``h_index`` alone is not used because its
    scale differs by field).
    """
    stats = source.get("summary_stats")
    citedness: float | None = None
    if isinstance(stats, dict):
        citedness = _as_float(stats.get("2yr_mean_citedness"))
    works = _as_int(source.get("works_count"))
    if citedness is None:
        return VenueIndicator.UNKNOWN
    if citedness >= cfg.strong_min_citedness:
        return VenueIndicator.STRONG
    if citedness < cfg.weak_max_citedness and works is not None and works > cfg.weak_min_works:
        return VenueIndicator.WEAK
    return VenueIndicator.NORMAL


def venue_legit(
    indicator: VenueIndicator, *, predatory: bool, user_quality: VenueQuality | None
) -> bool:
    """The "venue legit" condition of certain-pass.

    Conservative: a predatory or discontinued venue is never legit; the user's quartile wins
    when present (Q1-Q3 legit, Q4 or unranked not); otherwise ``strong``/``normal`` are legit and
    ``weak``/``unknown`` are not. Preprint servers and journals the profile follows are handled
    by the caller (they are legit regardless of the indicator).
    """
    if predatory:
        return False
    if user_quality is not None:
        if user_quality.discontinued:
            return False
        if user_quality.quartile_best is not None:
            return 1 <= user_quality.quartile_best <= 3
        return False
    return indicator in (VenueIndicator.STRONG, VenueIndicator.NORMAL)
