"""Scoring table tests: phrases, exclude, penalty, noise, categories, bonuses,
pass rule."""

from __future__ import annotations

import pytest

from kallimachos.config.schema import ScoringConfig
from kallimachos.interfaces import Author, BonusWeight, MultWeight, RunKind, Weights
from kallimachos.profile.schema import Channel, Keywords, Profile
from kallimachos.scoring.topic import author_matches, matched_phrases, phrase_count, score
from tests.unit.test_scoring_support import load_profile, make_record, scored

W = ScoringConfig().weights


# ---------------------------------------------------------------- phrase rules


def test_phrase_count_whole_phrase_only() -> None:
    assert phrase_count("Tidal harmonic analysis of gauges", "tidal harmonic") == 1
    assert phrase_count("Tidal harmonics analysis", "tidal harmonic") == 0  # not a whole token
    assert phrase_count("tidal-harmonic and TIDAL HARMONIC", "tidal harmonic") == 2


def test_nested_phrase_not_double_counted() -> None:
    hits = matched_phrases(
        "On the giant impact hypothesis", ["giant impact", "giant impact hypothesis"]
    )
    assert hits == ["giant impact hypothesis"]


def test_single_letter_token_is_case_sensitive() -> None:
    assert phrase_count("Structure of the K-loop", "K-loop") == 1
    assert phrase_count("A Loop-Space construction", "A-loop") == 0
    assert phrase_count("the A-loop property", "A-loop") == 1


# ---------------------------------------------------------------- core / broad / exclude


def test_core_scores_and_cap() -> None:
    profile = load_profile()
    rec = make_record(
        "Tidal harmonic constituent estimation with nodal modulation",
        "We fit each harmonic constituent and the tidal constituent set to tide gauge data.",
    )
    s = scored(rec, profile)
    cs = s.by_channel["T"]
    # title: "tidal harmonic", "nodal modulation" (harmonic constituent is matched too -> min 2)
    assert cs.core == W.title_core * 2 + W.abstract_core * 2
    assert cs.kw == min(cs.core + cs.broad, W.channel_cap)
    assert s.main_channel == "T"
    assert s.passes


def test_broad_only_needs_title_hit_or_two_abstract_hits() -> None:
    profile = load_profile()
    weak = make_record("An unrelated title", "We mention a tide gauge once.")
    assert "T" not in scored(weak, profile).by_channel
    ok = make_record("An unrelated title", "A tide gauge near an amphidromic point.")
    assert scored(ok, profile).by_channel["T"].broad == W.abstract_broad * 2


def test_exclude_cancels_without_core_but_not_with_core() -> None:
    profile = load_profile()
    cancelled = make_record("Sea level and the harmonic oscillator", "tide gauge data")
    s = scored(cancelled, profile)
    assert "T" not in s.by_channel
    assert "T" in s.cancelled_channels
    kept = make_record("Tidal harmonic view of the harmonic oscillator", None)
    assert "T" in scored(kept, profile).by_channel


def test_penalty_floor() -> None:
    profile = load_profile()
    rec = make_record(
        "Tidal harmonic study of nodal modulation",
        "blockchain sentiment analysis blockchain sentiment analysis",
    )
    cs = scored(rec, profile).by_channel["T"]
    assert cs.penalty == W.penalty_floor  # two penalty phrases, floor -6
    assert cs.raw == W.title_core * 2 + W.penalty_floor


def test_noise_factor_applies_with_title_hit_or_two_abstract_terms() -> None:
    cfg = ScoringConfig()
    profile = load_profile()
    clean = make_record("Tidal harmonic study", "nodal modulation")
    noisy = make_record("Tidal harmonic study of the stock market", "nodal modulation")
    base = scored(clean, profile, cfg=cfg).by_channel["T"]
    hit = scored(noisy, profile, cfg=cfg).by_channel["T"]
    assert hit.noise and not base.noise
    assert hit.final == pytest.approx(base.final * cfg.noise_factor, rel=1e-6)
    one_abstract = make_record("Tidal harmonic study", "nodal modulation of the stock market")
    assert not scored(one_abstract, profile, cfg=cfg).by_channel["T"].noise
    two_abstract = make_record(
        "Tidal harmonic study", "nodal modulation of the stock market and cryptocurrency"
    )
    assert scored(two_abstract, profile, cfg=cfg).by_channel["T"].noise


# ---------------------------------------------------------------- categories


def test_arxiv_category_bonus_and_mismatch_factor() -> None:
    profile = load_profile()
    plain = make_record("Tidal harmonic study", None)
    primary = make_record(
        "Tidal harmonic study", None, categories=["physics.ao-ph"], sources={"arxiv"}
    )
    secondary = make_record(
        "Tidal harmonic study", None, categories=["physics.geo-ph"], sources={"arxiv"}
    )
    mismatch = make_record("Tidal harmonic study", None, categories=["cs.LG"], sources={"arxiv"})
    p0 = scored(plain, profile).by_channel["T"]
    p1 = scored(primary, profile).by_channel["T"]
    p2 = scored(secondary, profile).by_channel["T"]
    pm = scored(mismatch, profile).by_channel["T"]
    assert p1.category_bonus == W.arxiv_primary_category
    assert p2.category_bonus == W.arxiv_secondary_category
    assert pm.kw == pytest.approx(p0.kw * W.arxiv_category_mismatch_factor)
    assert pm.final < p0.final < p2.final < p1.final


def test_msc_bonus_only_in_weekly() -> None:
    profile = load_profile()
    rec = make_record("Tidal harmonic study", None, msc=["86A05", "76B15"])
    daily = scored(rec, profile, run_kind=RunKind.DAILY).by_channel["T"]
    weekly = scored(rec, profile, run_kind=RunKind.WEEKLY).by_channel["T"]
    assert daily.category_bonus == 0
    assert weekly.category_bonus == W.msc_primary_main
    second = make_record("Tidal harmonic study", None, msc=["76B15"])
    assert (
        scored(second, profile, run_kind=RunKind.WEEKLY).by_channel["T"].category_bonus
        == W.msc_secondary
    )


def test_priority_multiplier_and_threshold() -> None:
    profile = load_profile()
    cfg = ScoringConfig()
    rec = make_record("Moon forming impact simulations", None)
    s = scored(rec, profile, cfg=cfg)
    cs = s.by_channel["M"]
    assert cs.final == pytest.approx(cs.raw * cfg.priority_multiplier[2])
    assert s.threshold == cfg.threshold[2]


# ---------------------------------------------------------------- bonuses


def test_always_show_author_is_mandatory_plus_100() -> None:
    profile = load_profile()
    rec = make_record("Unrelated bird migration note", None, authors=["E. Mentor"])
    s = scored(rec, profile)
    assert s.always_show and s.mandatory and s.passes
    assert s.bonuses["always_show"] == W.always_show_author
    assert s.total == W.always_show_author


def test_follow_author_bonus_and_learned_bonus() -> None:
    profile = load_profile()
    rec = make_record("Unrelated note", None, authors=["Example Verifiedsmith"])
    s = scored(rec, profile)
    assert s.bonuses["follow_author"] == W.follow_author
    assert s.passes and s.verified_follow_author
    assert s.channels == ["T"]  # channel hint from the followed author
    weights = Weights(author={"example verifiedsmith": BonusWeight(2.0)})
    assert scored(rec, profile, weights=weights).bonuses["follow_author"] == W.follow_author + 2.0


def test_author_name_matching_rules() -> None:
    assert author_matches("E. Mentor", name="Example Mentor") == "name"
    assert author_matches("Example Mentor", name="Example Mentor") == "name"
    assert author_matches("Other Mentor", name="Example Mentor") is None
    assert (
        author_matches(
            "X Y", name="Someone", record_orcid="0000-0000-0000-0001", orcid="0000-0000-0000-0001"
        )
        == "id"
    )


def test_seed_and_foundational_citation_caps() -> None:
    profile = load_profile()
    rec = make_record("Unrelated note", None, referenced_works=["W9000000001"])
    s = scored(rec, profile)
    assert s.bonuses["seed_citation"] == W.seed_citation
    assert s.passes and s.channels == ["T"]
    many = scored(
        make_record("Unrelated note", None, referenced_works=["W9000000001", "W9000000002"]),
        profile,
    )
    assert many.bonuses["seed_citation"] == W.seed_citation + W.seed_citation_extra
    s5 = score(
        make_record("Unrelated note", None), profile, Weights(), ScoringConfig(), seed_citations=9
    )
    assert s5.bonuses["seed_citation"] == W.seed_citation_cap
    f = score(
        make_record("Unrelated note", None),
        profile,
        Weights(),
        ScoringConfig(),
        foundational_citations=4,
    )
    assert f.bonuses["foundational_citation"] == W.foundational_citation_cap
    assert not f.passes  # foundational citation alone does not pass


def test_full_journal_mandatory_and_topic_filtered_reason_only() -> None:
    profile = load_profile()
    full = scored(make_record("Unrelated note", None, issn=["1111-0001"]), profile)
    assert full.full_journal and full.mandatory and full.passes
    assert full.bonuses["full_journal"] == W.full_journal
    assert full.channels == ["T"]
    filtered = scored(make_record("Unrelated note", None, issn=["1111-0002"]), profile)
    assert filtered.topic_filtered_journal and not filtered.mandatory and not filtered.passes
    assert any("topic-filtered journal" in r for r in filtered.reasons)


# ---------------------------------------------------------------- pass rule


def test_pass_rule_threshold() -> None:
    profile = load_profile()
    below = make_record("Nothing special", "a tide gauge and an amphidromic point")  # broad 2
    s = scored(below, profile)
    assert s.main_channel == "T" and not s.passes
    above = make_record("Tidal harmonic study", None)  # core 6 >= 4
    assert scored(above, profile).passes


def test_weekly_citation_alone_passes_only_for_flagged_channels() -> None:
    profile = load_profile()
    # seed W9000000001 belongs to T (citation_alone_passes false), W9000000002 to M (true)
    t_only = make_record("Unrelated note", None, referenced_works=["W9000000001"])
    m_only = make_record("Unrelated note", None, referenced_works=["W9000000002"])
    assert scored(t_only, profile, run_kind=RunKind.DAILY).passes
    assert not scored(t_only, profile, run_kind=RunKind.WEEKLY).passes
    assert scored(m_only, profile, run_kind=RunKind.WEEKLY).passes


def test_keywords_replace_missing_abstract() -> None:
    profile = load_profile()
    rec = make_record("Nothing special", None)
    rec.keywords = ["tidal harmonic", "nodal modulation"]
    assert scored(rec, profile).by_channel["T"].core == W.abstract_core * 2


def test_learned_channel_and_phrase_and_venue_multipliers() -> None:
    profile = load_profile()
    rec = make_record("Tidal harmonic study", None, issn=["1111-0002"])
    base = scored(rec, profile).topic_score
    down = scored(rec, profile, weights=Weights(channel={"T": MultWeight(0.5)})).topic_score
    assert down == pytest.approx(base * 0.5)
    phrase = scored(
        rec, profile, weights=Weights(phrase={"T/tidal harmonic": MultWeight(1.5)})
    ).topic_score
    assert phrase == pytest.approx(base * 1.5)
    venue = scored(rec, profile, weights=Weights(venue={"1111-0002": MultWeight(0.8)}))
    assert venue.topic_score == pytest.approx(base * 0.8)


def test_minimal_profile_without_channels_scores_nothing() -> None:
    profile = Profile(channels=[Channel(id="Z", name="Z", folder="Z", keywords=Keywords())])
    s = score(make_record("Anything", None), profile, Weights(), ScoringConfig())
    assert s.main_channel is None and not s.passes and s.total == 0


def test_score_reasons_never_contain_identifiers_of_record_authors() -> None:
    profile = load_profile()
    rec = make_record("Tidal harmonic study", None)
    rec.authors = [Author(name="Example Author A", orcid="0000-0000-0000-0002")]
    s = scored(rec, profile)
    assert all("0000-0000-0000-0002" not in r for r in s.reasons)
