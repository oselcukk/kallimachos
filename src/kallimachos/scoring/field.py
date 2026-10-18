"""Field classification helper for rules.

This is chassis, not domain policy: it maps arXiv categories, MSC codes and venue names onto
``math | math_phys | physics | non_physics | unknown`` so that profile YAML rules can refer to a
field class (``when: {field_class: physics}``). Field-specific decisions (which classes to drop,
exceptions for journals or authors) live in the profile, never here.
"""

from __future__ import annotations

import re

from kallimachos.interfaces import FieldClass, Record

PHYSICS_ARXIV_PREFIXES: tuple[str, ...] = (
    "hep-",
    "gr-qc",
    "astro-ph",
    "cond-mat",
    "quant-ph",
    "nucl-",
    "physics",
    "nlin",
)
MATH_PHYS_ARXIV: frozenset[str] = frozenset({"math-ph", "math.MP"})

# MSC 2020 two-digit classes that belong to mathematical physics / applied physics.
MATH_PHYS_MSC: frozenset[str] = frozenset(
    {"70", "74", "76", "78", "80", "81", "82", "83", "85", "86"}
)

_VENUE_MATH_PHYS = re.compile(r"mathematical physics|math(?:ematical)?[\s-]*phys", re.I)
_VENUE_PHYSICS = re.compile(r"\bphysic|\bphysik|\bastronom|\bastrophys|\boptic", re.I)
_VENUE_MATH = re.compile(
    r"mathemati|\balgebra|\bgeometr|\btopolog|number theory|\banalysis\b|combinator|"
    r"\bprobability\b|\bstatistic|\blogic\b|\bcryptolog",
    re.I,
)


def _arxiv_categories(record: Record) -> tuple[str | None, set[str]]:
    cats = set(record.categories)
    primary = record.primary_category
    if primary is None and record.categories:
        primary = record.categories[0]
    if primary:
        cats.add(primary)
    return primary, cats


def _is_math_category(cat: str) -> bool:
    return cat.startswith("math.") and cat not in MATH_PHYS_ARXIV


def _is_physics_category(cat: str) -> bool:
    return cat.startswith(PHYSICS_ARXIV_PREFIXES)


def _from_msc(record: Record) -> FieldClass | None:
    if not record.msc:
        return None
    classes = {code[:2] for code in record.msc if len(code) >= 2 and code[:2].isdigit()}
    if not classes:
        return None
    if classes <= MATH_PHYS_MSC:
        return FieldClass.MATH_PHYS
    return FieldClass.MATH


def _from_venue(venue: str | None) -> FieldClass | None:
    if not venue:
        return None
    if _VENUE_MATH_PHYS.search(venue):
        return FieldClass.MATH_PHYS
    is_physics = bool(_VENUE_PHYSICS.search(venue))
    is_math = bool(_VENUE_MATH.search(venue))
    if is_physics and is_math:
        return FieldClass.MATH_PHYS
    if is_physics:
        return FieldClass.PHYSICS
    if is_math:
        return FieldClass.MATH
    return None


def classify(record: Record) -> FieldClass:
    """``math | math_phys | physics | non_physics | unknown`` from categories, MSC and venue.

    Order: arXiv primary category (math.* -> math, math-ph -> math_phys, physics archives ->
    physics unless cross-listed in math.* -> math_phys, other archives -> non_physics unless
    cross-listed in math.* -> math); then MSC codes; then the venue name; else ``unknown``.
    """
    primary, cats = _arxiv_categories(record)
    if primary:
        cross_math = any(_is_math_category(c) for c in cats if c != primary)
        if _is_math_category(primary):
            return FieldClass.MATH
        if primary in MATH_PHYS_ARXIV:
            return FieldClass.MATH_PHYS
        if _is_physics_category(primary):
            if cross_math or (cats & MATH_PHYS_ARXIV):
                return FieldClass.MATH_PHYS
            return FieldClass.PHYSICS
        # cs.*, q-bio.*, stat.*, econ.*, eess.* ...
        if cross_math:
            return FieldClass.MATH
        by_msc = _from_msc(record)
        if by_msc is FieldClass.MATH:
            return FieldClass.MATH
        return FieldClass.NON_PHYSICS
    by_msc = _from_msc(record)
    if by_msc is not None:
        return by_msc
    by_venue = _from_venue(record.venue)
    if by_venue is not None:
        return by_venue
    return FieldClass.UNKNOWN
