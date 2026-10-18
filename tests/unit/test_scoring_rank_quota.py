"""Ranking and journal quota."""

from __future__ import annotations

import datetime as dt

from kallimachos.config.schema import RankingConfig
from kallimachos.scoring.quota import apply_journal_quota
from kallimachos.scoring.rank import rank, rank_and_quota, renumber, sort_candidates
from tests.unit.test_scoring_support import load_profile, simple_candidate


def test_sort_order_score_priority_date_and_always_show_on_top() -> None:
    profile = load_profile()
    older = simple_candidate("older", 8.0, date=dt.date(2026, 10, 1))
    newer = simple_candidate("newer", 8.0, date=dt.date(2026, 10, 10))
    m_same = simple_candidate("m", 8.0, channels=["M"], date=dt.date(2026, 10, 12))
    top = simple_candidate("top", 12.0)
    star = simple_candidate("star", 3.0, always_show=True)
    ordered = sort_candidates([older, m_same, newer, top, star], profile)
    assert [c.id for c in ordered] == ["star", "top", "newer", "older", "m"]


def test_rank_truncates_and_keeps_certain_pass() -> None:
    profile = load_profile()
    cfg = RankingConfig(candidates=5, main_items=3)
    cands = [simple_candidate(f"c{i}", 20.0 - i) for i in range(8)]
    cands[7].certain_pass = True  # lowest score, must stay in
    ranked = rank(cands, profile, cfg)
    assert len(ranked) == 5
    assert [c.no for c in ranked] == [1, 2, 3, 4, 5]
    assert "c7" in {c.id for c in ranked}
    assert "c4" not in {c.id for c in ranked}  # the last ordinary slot gave way


def test_renumber_from_one() -> None:
    cands = [simple_candidate("a", 1.0), simple_candidate("b", 2.0)]
    assert [c.no for c in renumber(cands)] == [1, 2]


def test_quota_pulls_two_journals_into_main_and_caps_pool() -> None:
    profile = load_profile()
    cfg = RankingConfig(candidates=10, main_items=4, journal_quota=2, journal_quota_pool=3)
    ordinary = [simple_candidate(f"o{i}", 30.0 - i) for i in range(10)]
    journals = [simple_candidate(f"j{i}", 5.0 - i * 0.1, full_journal=True) for i in range(5)]
    noisy_journal = simple_candidate("jn", 4.0, full_journal=True, noise=True)
    ordered = rank(ordinary + journals + [noisy_journal], profile, cfg, truncate=False)
    selected, not_in_main = apply_journal_quota(ordered, cfg)
    assert len(selected) == cfg.candidates
    main = selected[: cfg.main_items]
    main_journals = [c for c in main if c.full_journal]
    assert [c.id for c in main_journals] == ["j0", "j1"]
    assert all(c.quota for c in main_journals)
    assert sum(1 for c in selected if c.full_journal and not c.noise) == cfg.journal_quota_pool
    assert not_in_main == ["j2"]
    assert "jn" not in {c.id for c in selected}  # noisy journal is not a quota journal
    # displaced ordinary items moved down, never out of the top scorers
    assert [c.id for c in main if not c.full_journal] == ["o0", "o1"]
    assert selected[cfg.main_items].id == "o2"


def test_quota_never_displaces_always_show_or_certain_pass() -> None:
    profile = load_profile()
    cfg = RankingConfig(candidates=6, main_items=3, journal_quota=2, journal_quota_pool=6)
    star = simple_candidate("star", 50.0, always_show=True)
    certain = simple_candidate("sure", 20.0, certain=True)
    other = simple_candidate("other", 19.0)
    journals = [simple_candidate(f"j{i}", 5.0 - i, full_journal=True) for i in range(2)]
    ordered = rank([star, certain, other, *journals], profile, cfg, truncate=False)
    selected, _ = apply_journal_quota(ordered, cfg)
    main_ids = [c.id for c in selected[: cfg.main_items]]
    assert "star" in main_ids and "sure" in main_ids
    assert "j0" in main_ids and "other" not in main_ids


def test_rank_and_quota_keeps_certain_pass_within_candidates() -> None:
    profile = load_profile()
    cfg = RankingConfig(candidates=4, main_items=2, journal_quota=1, journal_quota_pool=2)
    cands = [simple_candidate(f"c{i}", 20.0 - i) for i in range(6)]
    cands[5].certain_pass = True
    journal = simple_candidate("j", 1.0, full_journal=True)
    selected, not_in_main = rank_and_quota([*cands, journal], profile, cfg)
    ids = [c.id for c in selected]
    assert len(selected) == 4 and ids[0] == "c0"
    assert "c5" in ids and "j" in ids
    assert [c.no for c in selected] == [1, 2, 3, 4]
    assert not_in_main == []
