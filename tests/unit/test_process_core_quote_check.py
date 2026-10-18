"""process.quote and process.check: cleaning, exit codes, quote location, scope,
split numbers, reference heading, false alarms, schema, importance AND-rule, failure threshold."""

from __future__ import annotations

import pytest

from kallimachos.config.schema import ProcessConfig
from kallimachos.interfaces import CardItem, FalseAlarm, ItemStatus, ItemType
from kallimachos.process import check, quote
from kallimachos.process.card import item_rule_errors
from tests.unit.conftest_pdf import PAGE_1, PAGE_2, QUOTE_OK, fake_store, make_card  # noqa: F401

CFG = ProcessConfig()


# --- quote -------------------------------------------------------------------------------------


def test_clean_text_rules() -> None:
    raw = "The ﬁrst exam-\nple con­tains\x0ccontrol  chars\tand   spaces"
    assert quote.clean_text(raw) == "The first example contains control chars and spaces"
    assert quote.clean_text("é") == "é"  # NFC


def test_quote_found_with_end_and_length() -> None:
    pages = [PAGE_1, PAGE_2]
    r = quote.quote(pages, 1, "For every fictional", end="illustrative lattice.")
    assert r.exit_code == 0
    assert r.text == QUOTE_OK
    r2 = quote.quote(pages, 1, "for EVERY   fictional", length=20)  # case/whitespace tolerant
    assert r2.exit_code == 0
    assert r2.text == QUOTE_OK[:20].rstrip()
    assert r2.start_offset == r.start_offset


def test_quote_exit_codes() -> None:
    pages = [PAGE_1, PAGE_2]
    assert quote.quote(pages, 1, "not in the text at all").exit_code == 1
    assert quote.quote(pages, 1, "For every", end="missing end phrase").exit_code == 1
    assert quote.quote(pages, 3, "For every").exit_code == 2  # page out of range
    assert quote.quote(pages, 0, "For every").exit_code == 2
    assert quote.quote(pages, 1, "   ").exit_code == 2
    assert quote.quote(pages, 1, "For every", end="x", length=5).exit_code == 2


# --- find_quote ----------------------------------------------------------------------------------


def test_find_quote_found_page_fixed_not_found() -> None:
    pages = [PAGE_1, PAGE_2, "Unrelated page about something else entirely."]
    ok = check.find_quote(pages, QUOTE_OK, 1, threshold=90)
    assert ok.found and ok.page_fixed is None
    fixed = check.find_quote(pages, QUOTE_OK, 3, threshold=90)
    assert fixed.found and fixed.page_fixed == 1
    missing = check.find_quote(
        pages, "a sentence that appears nowhere in this work", 1, threshold=90
    )
    assert not missing.found
    hyphen = check.find_quote(
        ["the illus-\ntrative lattice holds"], "illustrative lattice holds", 1, threshold=90
    )
    assert hyphen.found


def test_find_quote_across_page_boundary() -> None:
    first, second = QUOTE_OK[:35], QUOTE_OK[35:]
    pages = ["x " * 30 + first, second + " y " * 30]
    r = check.find_quote(pages, QUOTE_OK, 1, threshold=90)
    assert r.found and r.page_fixed is None


# --- scope ---------------------------------------------------------------------------------------


def test_scope_headings_rules() -> None:
    pages = [
        "Intro text.\n\nTheorem 2.1. Something holds.\nby Theorem 3.5 we get\nmore text.\n"
        "Lemma 4.2 (Key lemma). Stated here.\nProblem 5.\n4. Is it true?\n\n"
        "This follows from\nCorollary 7.1 of the earlier work.\n",
        "References\n\n[1] Theorem 9.9 appears only in a title here.\n",
    ]
    heads = {(h.word, h.number, h.page) for h in check.scope_headings(pages)}
    assert ("theorem", "2.1", 1) in heads
    assert ("lemma", "4.2", 1) in heads
    assert ("problem", "5.4", 1) in heads  # split number joined
    assert not any(h[1] == "7.1" for h in heads)  # previous line mid-sentence
    assert not any(h[1] == "9.9" for h in heads)  # reference list not counted
    assert not any(h[1] == "3.5" for h in heads)  # not at line start


def test_reference_count_estimate() -> None:
    pages = ["Body.\n", "References\n[1] A.\n[2] B.\n[3] C.\n"]
    assert check.estimate_reference_count(pages) == 3
    assert check.estimate_reference_count(["no refs"]) is None
    numbered = ["Bibliography\n1. A, 2001.\n2. B, 2002.\n"]
    assert check.estimate_reference_count(numbered) == 2


# --- check_card ----------------------------------------------------------------------------------


def test_check_passes_on_matching_card() -> None:
    report = check.check_card(make_card(), [PAGE_1, PAGE_2], CFG)
    assert report.result == "check_passed", report
    assert report.quotes[0].found
    assert report.missing_items == []
    assert report.reference_count_estimate == 1


def test_check_fails_on_not_found_quote() -> None:
    card = make_card(
        items=[CardItem(ItemType.THEOREM, "Theorem 1.1", 1, "z" * 50, "s", number="1.1")]
    )
    report = check.check_card(card, [PAGE_1, PAGE_2], CFG)
    assert report.result == "check_failed"
    assert report.not_found_ratio == 1.0


def test_check_fixes_page_and_still_passes() -> None:
    card = make_card(
        items=[CardItem(ItemType.THEOREM, "Theorem 1.1", 2, QUOTE_OK, "s", number="1.1")]
    )
    report = check.check_card(card, [PAGE_1, PAGE_2], CFG)
    assert report.quotes[0].found and report.quotes[0].page_fixed == 1
    assert report.schema_errors == []
    assert report.result == "check_passed"


def test_missing_heavy_item_fails_and_false_alarm_closes_it() -> None:
    pages = [PAGE_1 + "\nConjecture 2.3. Every fictional lattice is illustrative.\n", PAGE_2]
    report = check.check_card(make_card(), pages, CFG)
    assert "Conjecture 2.3 (p. 1)" in report.missing_items
    assert report.result == "check_failed"
    closed = make_card(
        false_alarms=[FalseAlarm(page=1, phrase="Conjecture 2.3", reason="restates [1]")]
    )
    report2 = check.check_card(closed, pages, CFG)
    assert report2.result == "check_passed"


def test_missing_supporting_item_is_reported_but_not_fatal() -> None:
    pages = [PAGE_1 + "\nLemma 1.2. A helper statement for the fictional lattice.\n", PAGE_2]
    report = check.check_card(make_card(), pages, CFG)
    assert "Lemma 1.2 (p. 1)" in report.missing_items
    assert report.result == "check_passed"


def test_open_problem_phrase_needs_cover() -> None:
    pages = [PAGE_1 + "\nWhether this extends to all parameters remains open.\n", PAGE_2]
    report = check.check_card(make_card(), pages, CFG)
    assert any("remains open" in w for w in report.scope_warnings)
    assert report.result == "check_failed"
    item = CardItem(
        ItemType.OPEN_PROBLEM,
        "Open problem",
        1,
        "Whether this extends to all parameters remains open in the fictional setting.",
        "Extension to all parameters.",
        status=ItemStatus.OPEN,
        concreteness="concrete",
    )
    covered = make_card(items=[*make_card().items, item])
    pages2 = [pages[0] + " in the fictional setting.", PAGE_2]
    assert check.check_card(covered, pages2, CFG).result == "check_passed"


def test_importance_and_rule_and_item_rules() -> None:
    quoted_def = CardItem(
        ItemType.DEFINITION,
        "Definition 1.0",
        1,
        QUOTE_OK,
        "s",
        quoted_from_other=True,
        importance="main",
    )
    errors = item_rule_errors(make_card(items=[quoted_def]))
    assert any("supporting" in e for e in errors)
    plain_def = CardItem(ItemType.DEFINITION, "Definition 1.0", 1, QUOTE_OK, "s", importance="main")
    assert item_rule_errors(make_card(items=[plain_def])) == []  # OR would flag this; AND does not
    other_main = CardItem(ItemType.THEOREM, "Theorem 1.1", 1, QUOTE_OK, "s", quoted_from_other=True)
    assert item_rule_errors(make_card(items=[other_main])) == []
    open_no_status = CardItem(ItemType.QUESTION, "Question 2", 1, QUOTE_OK, "s")
    assert any("without status" in e for e in item_rule_errors(make_card(items=[open_no_status])))
    solved = CardItem(
        ItemType.CONJECTURE, "Conjecture 3", 1, QUOTE_OK, "s", status=ItemStatus.SOLVED_ELSEWHERE
    )
    assert any("solution_cites" in e for e in item_rule_errors(make_card(items=[solved])))
    claim = CardItem(ItemType.CLAIMED_SOLUTION, "Claim", 1, QUOTE_OK, "s", cites=["[9]"])
    errs = item_rule_errors(make_card(items=[claim]))
    assert any("solution_kind" in e for e in errs)
    assert any("target" in e for e in errs)
    assert any("unknown reference key" in e for e in errs)


def test_schema_error_fails_check() -> None:
    bad_page = CardItem(ItemType.THEOREM, "Theorem 1.1", 9, QUOTE_OK, "s", number="1.1")
    report = check.check_card(make_card(items=[bad_page]), [PAGE_1, PAGE_2], CFG)
    assert any("outside 1-2" in e for e in report.schema_errors)
    assert report.result == "check_failed"


def test_failure_threshold_five_percent() -> None:
    good = [
        CardItem(ItemType.THEOREM, "Theorem 1.1", 1, QUOTE_OK, "s", number="1.1") for _ in range(19)
    ]
    bad = CardItem(ItemType.REMARK, "Remark", 1, "w" * 50, "s")
    report = check.check_card(make_card(items=[*good, bad]), [PAGE_1, PAGE_2], CFG)
    assert report.not_found_ratio == pytest.approx(0.05)
    assert report.result == "check_passed"  # exactly 5 % is not "more than 5 %"
    report2 = check.check_card(make_card(items=[*good[:18], bad]), [PAGE_1, PAGE_2], CFG)
    assert report2.result == "check_failed"


def test_turkish_phrase_file_loads() -> None:
    patterns = check.load_open_problem_patterns(["en", "tr"])
    assert len(patterns) > 12
    assert any(p.search("bu soru açık kalır") for p in patterns)
