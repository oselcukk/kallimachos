"""Field classification and venue quality helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kallimachos.config.schema import VenueIndicatorConfig
from kallimachos.interfaces import FieldClass, SchemaViolation, VenueIndicator
from kallimachos.scoring.field import classify
from kallimachos.scoring.venue_quality import (
    VenueQuality,
    indicator_from_openalex,
    load_user_quality,
    venue_legit,
)
from tests.unit.test_scoring_support import FIXTURES, make_record


@pytest.mark.parametrize(
    ("categories", "primary", "expected"),
    [
        (["math.NT"], "math.NT", FieldClass.MATH),
        (["math-ph", "math.MP"], "math-ph", FieldClass.MATH_PHYS),
        (["hep-th"], "hep-th", FieldClass.PHYSICS),
        (["hep-th", "math.DG"], "hep-th", FieldClass.MATH_PHYS),
        (["quant-ph", "math-ph"], "quant-ph", FieldClass.MATH_PHYS),
        (["cs.CR"], "cs.CR", FieldClass.NON_PHYSICS),
        (["cs.CR", "math.GR"], "cs.CR", FieldClass.MATH),
        (["physics.ao-ph"], None, FieldClass.PHYSICS),
    ],
)
def test_classify_from_arxiv(
    categories: list[str], primary: str | None, expected: FieldClass
) -> None:
    rec = make_record("t", None, categories=categories, primary_category=primary)
    assert classify(rec) is expected


def test_classify_from_msc_and_venue() -> None:
    assert classify(make_record("t", None, msc=["11M06"])) is FieldClass.MATH
    assert classify(make_record("t", None, msc=["81Q10", "82B20"])) is FieldClass.MATH_PHYS
    assert classify(make_record("t", None, venue="Example Journal of Number Theory")) is (
        FieldClass.MATH
    )
    assert classify(make_record("t", None, venue="Example Physical Letters")) is FieldClass.PHYSICS
    assert classify(make_record("t", None, venue="Example Mathematical Physics Annals")) is (
        FieldClass.MATH_PHYS
    )
    assert classify(make_record("t", None, venue="Example Ocean Letters")) is FieldClass.UNKNOWN
    assert classify(make_record("t", None)) is FieldClass.UNKNOWN


def test_indicator_from_openalex_fixtures() -> None:
    cfg = VenueIndicatorConfig()
    strong = json.loads((FIXTURES / "openalex_source_strong.json").read_text(encoding="utf-8"))
    weak = json.loads((FIXTURES / "openalex_source_weak.json").read_text(encoding="utf-8"))
    assert indicator_from_openalex(strong, cfg) is VenueIndicator.STRONG
    assert indicator_from_openalex(weak, cfg) is VenueIndicator.WEAK
    assert indicator_from_openalex({"works_count": 10}, cfg) is VenueIndicator.UNKNOWN
    assert indicator_from_openalex({}, cfg) is VenueIndicator.UNKNOWN
    normal = {"summary_stats": {"2yr_mean_citedness": 0.6}, "works_count": 50}
    assert indicator_from_openalex(normal, cfg) is VenueIndicator.NORMAL
    small_low = {"summary_stats": {"2yr_mean_citedness": 0.1}, "works_count": 50}
    assert indicator_from_openalex(small_low, cfg) is VenueIndicator.NORMAL  # too few works
    custom = VenueIndicatorConfig(strong_min_citedness=5.0)
    assert indicator_from_openalex(strong, custom) is VenueIndicator.NORMAL
    odd = {"summary_stats": {"2yr_mean_citedness": "n/a"}, "works_count": "many"}
    assert indicator_from_openalex(odd, cfg) is VenueIndicator.UNKNOWN


def test_load_user_quality(tmp_path: Path) -> None:
    quality = load_user_quality(FIXTURES / "venue_quality.json")
    assert quality["1111-0001"].quartile_best == 1
    assert quality["1111-0001"].quartile_for(2025) == 2
    assert quality["1111-0001"].quartile_for(2023) == 1  # nearest listed year
    assert quality["1111-0003"].quartile_for(None) == 4
    assert quality["1111-0004"].discontinued
    assert load_user_quality(tmp_path / "missing.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text('{"1111-0001": {"quartile_best": 1, "typo": 2}}', encoding="utf-8")
    with pytest.raises(SchemaViolation):
        load_user_quality(bad)
    bad.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(SchemaViolation):
        load_user_quality(bad)


def test_venue_legit_conditions() -> None:
    assert venue_legit(VenueIndicator.STRONG, predatory=True, user_quality=None) is False
    assert venue_legit(VenueIndicator.STRONG, predatory=False, user_quality=None) is True
    assert venue_legit(VenueIndicator.NORMAL, predatory=False, user_quality=None) is True
    assert venue_legit(VenueIndicator.WEAK, predatory=False, user_quality=None) is False
    assert venue_legit(VenueIndicator.UNKNOWN, predatory=False, user_quality=None) is False
    q1 = VenueQuality(quartile_best=1)
    q4 = VenueQuality(quartile_best=4)
    gone = VenueQuality(quartile_best=1, discontinued=True)
    assert venue_legit(VenueIndicator.UNKNOWN, predatory=False, user_quality=q1) is True
    assert venue_legit(VenueIndicator.STRONG, predatory=False, user_quality=q4) is False
    assert venue_legit(VenueIndicator.STRONG, predatory=False, user_quality=gone) is False
