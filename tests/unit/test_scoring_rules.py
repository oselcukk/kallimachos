"""Rule chain tests: exemptions beat drops, certain pass survives, every verdict
in the trace, YAML rules, unknown ``when`` key -> exit 2, field-specific policy as profile YAML."""

from __future__ import annotations

import datetime as dt

import pytest

from kallimachos.config.schema import ScoringConfig
from kallimachos.interfaces import (
    Candidate,
    ExitCode,
    FieldClass,
    RuleContext,
    RuleStage,
    SeenEntry,
    UsageError,
    VenueIndicator,
    Verdict,
    VerdictKind,
    WorkKind,
)
from kallimachos.profile.schema import ProfileRule
from kallimachos.records import paratext
from kallimachos.scoring.rules.base import EXEMPT_REASON, BaseRule, run_chain
from kallimachos.scoring.rules.core import (
    CertainPassRule,
    ParatextRule,
    PredatoryList,
    PredatoryVenueRule,
    PriorUserDropRule,
    RepositoryVenueRule,
    SeenRule,
    VenueQualityRule,
    default_rules,
)
from kallimachos.scoring.rules.loader import compile_when, rule_from_profile, rules_from_profile
from kallimachos.scoring.venue_quality import VenueQuality
from tests.unit.test_scoring_support import (
    candidate,
    context,
    load_profile,
    make_record,
    simple_candidate,
)


class AlwaysDrop(BaseRule):
    id = "always_drop"
    stage = RuleStage.DROP

    def apply(self, cand: Candidate, ctx: RuleContext) -> Verdict:
        return Verdict(VerdictKind.DROP, "test drop")


class AlwaysPass(BaseRule):
    id = "always_pass"
    stage = RuleStage.PASS

    def apply(self, cand: Candidate, ctx: RuleContext) -> Verdict:
        cand.certain_pass = True
        return Verdict(VerdictKind.PASS, "test pass")


class AnnotateDrop(BaseRule):
    id = "annotate_tries_drop"
    stage = RuleStage.ANNOTATE

    def apply(self, cand: Candidate, ctx: RuleContext) -> Verdict:
        return Verdict(VerdictKind.DROP, "should be ignored")


@pytest.fixture(autouse=True)
def _fake_paratext(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the documented classifier contract instead of the real classifier."""

    def classify(record, *, extra_paratext=(), extra_repositories=()):  # type: ignore[no-untyped-def]
        title = (record.title or "").lower()
        venue = (record.venue or "").lower()
        if any(p in title for p in ("editorial board", "erratum", *extra_paratext)):
            return "paratext"
        if any(r in venue for r in ("zenodo", "figshare", *extra_repositories)):
            return "repository"
        return None

    monkeypatch.setattr(paratext, "classify", classify)


# ---------------------------------------------------------------- chain mechanics


def test_exemption_beats_drop_rules_and_is_traced() -> None:
    profile = load_profile()
    cand = simple_candidate("doi:10.0000/x.1", 9.0)
    ctx = context(profile, exempt={cand.id})
    verdict = run_chain(cand, ctx, [AlwaysDrop(), AlwaysPass()])
    assert verdict.kind is VerdictKind.PASS
    assert [(e.rule, e.verdict, e.reason) for e in cand.rule_trace] == [
        ("always_drop", VerdictKind.NEUTRAL, EXEMPT_REASON),
        ("always_pass", VerdictKind.PASS, "test pass"),
    ]


def test_always_show_author_is_exempt() -> None:
    profile = load_profile()
    cand = simple_candidate("doi:10.0000/x.2", 109.0, always_show=True)
    assert run_chain(cand, context(profile), [AlwaysDrop()]).kind is VerdictKind.NEUTRAL


def test_first_drop_wins_and_stops_the_chain() -> None:
    profile = load_profile()
    cand = simple_candidate("doi:10.0000/x.3", 9.0)
    verdict = run_chain(cand, context(profile), [AlwaysDrop(), AlwaysPass()])
    assert verdict.kind is VerdictKind.DROP and verdict.reason == "test drop"
    assert [e.rule for e in cand.rule_trace] == ["always_drop"]


def test_annotate_rule_cannot_drop() -> None:
    profile = load_profile()
    cand = simple_candidate("doi:10.0000/x.4", 9.0)
    assert run_chain(cand, context(profile), [AnnotateDrop()]).kind is VerdictKind.NEUTRAL
    assert cand.rule_trace[0].verdict is VerdictKind.NEUTRAL


# ---------------------------------------------------------------- core drop rules


def test_seen_rule_shown_drops_unshown_in_grace_competes_expired_drops() -> None:
    profile = load_profile()
    shown = SeenEntry(
        keys=["k1"],
        first_run="2026-10-10-daily",
        last_run="2026-10-10-daily",
        shown=True,
        shown_run="2026-10-10-daily",
        shown_no=2,
        kind=WorkKind.PREPRINT,
    )
    recent = SeenEntry(keys=["k2"], first_run="2026-10-15-daily", last_run="2026-10-15-daily")
    old = SeenEntry(keys=["k3"], first_run="2026-10-01-daily", last_run="2026-10-02-daily")
    ctx = context(profile, seen={"k1": shown, "k2": recent, "k3": old}, unshown_grace_days=7)
    rule = SeenRule()
    c1 = simple_candidate("a", 5.0)
    c1.keys = ["k1"]
    c2 = simple_candidate("b", 5.0)
    c2.keys = ["k2"]
    c3 = simple_candidate("c", 5.0)
    c3.keys = ["k3"]
    assert rule.apply(c1, ctx).kind is VerdictKind.DROP
    assert rule.apply(c2, ctx).kind is VerdictKind.NEUTRAL  # unshown candidate is not lost
    assert rule.apply(c3, ctx).kind is VerdictKind.DROP
    c4 = simple_candidate("d", 5.0)
    c4.keys = ["k1"]
    c4.kind = WorkKind.ARTICLE  # journal version of the shown preprint: once more
    verdict = rule.apply(c4, ctx)
    assert verdict.kind is VerdictKind.NEUTRAL and c4.note


def test_paratext_and_repository_rules() -> None:
    profile = load_profile()
    ctx = context(profile)
    editorial = simple_candidate("e", 5.0)
    editorial.title = "Editorial Board"
    assert ParatextRule().apply(editorial, ctx).kind is VerdictKind.DROP
    repo = simple_candidate("r", 5.0)
    repo.venue = "Zenodo"
    assert RepositoryVenueRule().apply(repo, ctx).kind is VerdictKind.DROP
    assert ParatextRule().apply(repo, ctx).kind is VerdictKind.NEUTRAL


def test_predatory_rule_from_list_and_user_extra() -> None:
    profile = load_profile()
    lst = PredatoryList(
        {
            "entries": [
                {"name_regex": "Example Predatory Journal", "doi_prefixes": ["10.99999/"]},
            ]
        }
    )
    ctx = context(profile, _predatory_list=lst)
    bad = simple_candidate("p", 5.0)
    bad.venue = "example predatory journal of everything"
    assert PredatoryVenueRule().apply(bad, ctx).kind is VerdictKind.DROP
    doi = simple_candidate("q", 5.0)
    doi.doi = "10.99999/abc"
    assert PredatoryVenueRule().apply(doi, ctx).kind is VerdictKind.DROP
    fine = simple_candidate("f", 5.0)
    fine.venue = "Example Tidal Review"
    assert PredatoryVenueRule().apply(fine, ctx).kind is VerdictKind.NEUTRAL
    extra = PredatoryList([], extra=["Bulk Proceedings"])
    ctx2 = context(profile, _predatory_list=extra)
    bulk = simple_candidate("b", 5.0)
    bulk.venue = "Example Bulk Proceedings"
    assert PredatoryVenueRule().apply(bulk, ctx2).kind is VerdictKind.DROP


def test_packaged_predatory_file_is_read_in_the_entries_format() -> None:
    """The packaged file (the archive's ``entries[]`` shape) must actually feed the rule."""
    lst = PredatoryList.load()
    assert lst.patterns, "packaged predatory_venues.json produced no patterns"
    omics = simple_candidate("o", 5.0)
    omics.venue = "OMICS Journal of Everything"
    assert lst.matches(omics) is not None


def test_venue_quality_rule_annotates_never_drops() -> None:
    profile = load_profile()
    rec = make_record(
        "Tidal harmonic study", None, issn=["1111-0001"], published=dt.date(2025, 3, 1)
    )
    cand = candidate(rec, profile, cid="doi:10.0000/v.1")
    ctx = context(
        profile,
        records={cand.id: rec},
        user_quality={"1111-0001": VenueQuality(quartile_best=1, quartile_by_year={"2025": 2})},
    )
    verdict = VenueQualityRule().apply(cand, ctx)
    assert verdict.kind is VerdictKind.NEUTRAL
    assert cand.venue_quality_short == "Q2" and cand.quality_mark == "Q2"
    weak = candidate(
        make_record("Tidal harmonic study", None, issn=["1111-0009"]),
        profile,
        cid="doi:10.0000/v.2",
    )
    ctx2 = context(
        profile,
        records={weak.id: make_record("x", None, issn=["1111-0009"])},
        venue_indicator={"1111-0009": VenueIndicator.WEAK},
    )
    assert VenueQualityRule().apply(weak, ctx2).kind is VerdictKind.NEUTRAL
    assert weak.venue_quality_short == "weak"


def test_prior_user_drop_rule() -> None:
    profile = load_profile()
    cand = simple_candidate("doi:10.0000/u.1", 5.0)
    assert PriorUserDropRule().apply(cand, context(profile, prior_user_drops={cand.id})).kind is (
        VerdictKind.DROP
    )
    assert PriorUserDropRule().apply(cand, context(profile)).kind is VerdictKind.NEUTRAL


# ---------------------------------------------------------------- certain pass


def _strong_record(**kw):  # type: ignore[no-untyped-def]
    return make_record(
        "Tidal harmonic constituent estimation with nodal modulation",
        "Harmonic constituent and tidal constituent fits of tide gauge records.",
        **kw,
    )


def test_certain_pass_by_score_factor_for_preprint() -> None:
    profile = load_profile()
    rec = _strong_record(arxiv_id="2510.00001", sources={"arxiv"}, categories=["physics.ao-ph"])
    cand = candidate(rec, profile, cid="arXiv:2510.00001")
    cand.field_class = FieldClass.MATH_PHYS
    ctx = context(profile, records={cand.id: rec})
    verdict = run_chain(cand, ctx, default_rules(profile))
    assert verdict.kind is VerdictKind.PASS
    assert cand.certain_pass and "topic score" in (cand.certain_pass_reason or "")
    assert cand.rule_trace[-1].rule == "certain_pass"


def test_certain_pass_blocked_by_field_noise_or_venue() -> None:
    profile = load_profile()
    rec = _strong_record(arxiv_id="2510.00002", sources={"arxiv"})
    base = candidate(rec, profile, cid="arXiv:2510.00002")
    base.field_class = FieldClass.MATH
    ctx = context(profile, records={base.id: rec})
    assert CertainPassRule().apply(base, ctx).kind is VerdictKind.PASS

    wrong_field = candidate(rec, profile, cid="arXiv:2510.00002")
    wrong_field.field_class = FieldClass.NON_PHYSICS
    assert CertainPassRule().apply(wrong_field, ctx).kind is VerdictKind.NEUTRAL

    noisy = candidate(rec, profile, cid="arXiv:2510.00002")
    noisy.field_class = FieldClass.MATH
    noisy.noise = True
    assert CertainPassRule().apply(noisy, ctx).kind is VerdictKind.NEUTRAL

    journal_rec = _strong_record(doi="10.0000/j.1", issn=["1111-0009"], kind=WorkKind.ARTICLE)
    unknown_venue = candidate(journal_rec, profile, cid="doi:10.0000/j.1")
    unknown_venue.field_class = FieldClass.MATH
    ctx_j = context(profile, records={unknown_venue.id: journal_rec})
    assert CertainPassRule().apply(unknown_venue, ctx_j).kind is VerdictKind.NEUTRAL
    ctx_strong = context(
        profile,
        records={unknown_venue.id: journal_rec},
        venue_indicator={"1111-0009": VenueIndicator.STRONG},
    )
    assert CertainPassRule().apply(unknown_venue, ctx_strong).kind is VerdictKind.PASS


def test_certain_pass_fields_and_factor_configurable() -> None:
    profile = load_profile()
    rec = _strong_record(arxiv_id="2510.00003", sources={"arxiv"})
    cand = candidate(rec, profile, cid="arXiv:2510.00003")
    cand.field_class = FieldClass.PHYSICS
    cfg = ScoringConfig(
        certain_pass_fields=[FieldClass.PHYSICS], certain_pass_factor_by_channel={"T": 10.0}
    )
    ctx = context(profile, records={cand.id: rec}, scoring_config=cfg)
    assert CertainPassRule().apply(cand, ctx).kind is VerdictKind.NEUTRAL  # factor too high
    cfg2 = ScoringConfig(certain_pass_fields=[FieldClass.PHYSICS])
    ctx2 = context(profile, records={cand.id: rec}, scoring_config=cfg2)
    assert CertainPassRule().apply(cand, ctx2).kind is VerdictKind.PASS


def test_certain_pass_candidate_survives_chain_and_model_cannot_remove_marker() -> None:
    """The chain never turns a certain pass into a drop afterwards; the flag is on the candidate
    (``select`` enforces it on the model output)."""
    profile = load_profile()
    rec = _strong_record(arxiv_id="2510.00004", sources={"arxiv"})
    cand = candidate(rec, profile, cid="arXiv:2510.00004")
    cand.field_class = FieldClass.MATH
    ctx = context(profile, records={cand.id: rec})
    run_chain(cand, ctx, default_rules(profile))
    assert cand.certain_pass
    assert all(e.verdict is not VerdictKind.DROP for e in cand.rule_trace)


# ---------------------------------------------------------------- YAML rules


def test_unknown_when_key_is_usage_error_exit_2() -> None:
    with pytest.raises(UsageError) as info:
        compile_when({"venue_regex": "x"})
    assert info.value.exit_code == ExitCode.USAGE
    assert "venue_regex" in str(info.value)
    with pytest.raises(UsageError):
        rule_from_profile(ProfileRule(id="bad", when={"title_regex": "("}), channel_id=None)
    with pytest.raises(UsageError):
        compile_when({"field_class": "chemistry"})
    with pytest.raises(UsageError):
        compile_when({})


def test_rules_from_profile_order_and_channel_scope() -> None:
    profile = load_profile()
    rules = rules_from_profile(profile)
    assert [r.id for r in rules] == [
        "t_no_plasma_without_core",
        "physics_unless_verified_author",
        "t_structural_pass",
    ]
    # the T channel rule does not touch an M candidate
    m_cand = simple_candidate("m", 7.0, channels=["M"])
    m_cand.title = "Plasma in the protolunar disk"
    assert rules[0].apply(m_cand, context(profile)).kind is VerdictKind.NEUTRAL
    t_cand = simple_candidate("t", 7.0, channels=["T"])
    t_cand.title = "Plasma waves"
    assert rules[0].apply(t_cand, context(profile)).kind is VerdictKind.DROP


def test_domain_policy_is_expressible_as_profile_yaml_not_core() -> None:
    """A fictional field's own rules (drop pure physics unless a verified colleague is an author;
    pass a structural-phrase paper in a legit venue) work purely from the profile YAML."""
    profile = load_profile()
    rules = default_rules(profile)

    # 1. physics paper, no verified colleague -> dropped by the profile rule, traced
    phys_rec = make_record(
        "Tidal harmonic forcing of plasma sheets",
        "nodal modulation",
        authors=["Someone Else"],
        categories=["physics.plasm-ph"],
        sources={"arxiv"},
        arxiv_id="2510.00010",
    )
    phys = candidate(phys_rec, profile, cid="arXiv:2510.00010")
    assert phys.field_class is FieldClass.PHYSICS
    ctx = context(profile, records={phys.id: phys_rec})
    verdict = run_chain(phys, ctx, rules)
    assert verdict.kind is VerdictKind.DROP
    assert verdict.reason == "physics paper without a verified colleague"
    assert any(e.rule == "physics_unless_verified_author" for e in phys.rule_trace)

    # 2. same paper with the verified colleague -> the exception in the YAML rule keeps it
    with_colleague = candidate(
        make_record(
            "Tidal harmonic forcing of plasma sheets",
            "nodal modulation",
            authors=["Example Verifiedsmith"],
            categories=["physics.plasm-ph"],
            sources={"arxiv"},
            arxiv_id="2510.00011",
        ),
        profile,
        cid="arXiv:2510.00011",
    )
    ctx2 = context(profile, records={with_colleague.id: make_record("x", None)})
    assert run_chain(with_colleague, ctx2, rules).kind is not VerdictKind.DROP

    # 3. structural-phrase paper below the certain-pass factor passes via the YAML pass rule
    weak_rec = make_record(
        "A constituent phase lattice for sea level records",
        None,
        arxiv_id="2510.00012",
        sources={"arxiv"},
        categories=["physics.ao-ph"],
    )
    weak = candidate(weak_rec, profile, cid="arXiv:2510.00012")
    assert weak.channels[0] == "T" and weak.score < 1.5 * weak.threshold
    weak.field_class = FieldClass.NON_PHYSICS  # core certain-pass would refuse this field
    ctx3 = context(profile, records={weak.id: weak_rec})
    verdict3 = run_chain(weak, ctx3, rules)
    assert verdict3.kind is VerdictKind.PASS and weak.certain_pass
    assert weak.certain_pass_reason == "constituent lattice paper in a legit venue"
    assert [e.rule for e in weak.rule_trace if e.verdict is VerdictKind.PASS] == [
        "t_structural_pass"
    ]


def test_combinators_all_any_not() -> None:
    pred = compile_when({"any": [{"title_regex": "alpha"}, {"not": {"abstract_regex": "beta"}}]})
    profile = load_profile()
    ctx = context(profile)
    c = simple_candidate("c", 1.0)
    c.title, c.abstract = "gamma", "beta"
    assert not pred(c, ctx)
    c.title = "alpha"
    assert pred(c, ctx)
    c.title, c.abstract = "gamma", "delta"
    assert pred(c, ctx)
    both = compile_when({"all": [{"title_regex": "a"}, {"context_channel": "T"}]})
    assert both(c, ctx)
    c.channels = ["M"]
    assert not both(c, ctx)
