"""Reply grammar: en/tr synonyms, H numbers, ask:<person>, multiple numbers,
daily/weekly never mixed, markdown checkbox tags."""

from __future__ import annotations

import pytest

from kallimachos.feedback.grammar import markdown_tag, parse
from kallimachos.interfaces import FeedbackTag


def test_english_single_reply() -> None:
    (reply,) = parse("3 good")
    assert reply.items == ["3"] and reply.tag is FeedbackTag.GOOD
    assert reply.note is None and not reply.ambiguous


@pytest.mark.parametrize(
    ("text", "tag"),
    [
        ("2 oku", FeedbackTag.READ),
        ("2 okuyacağım", FeedbackTag.READ),
        ("2 iyi", FeedbackTag.GOOD),
        ("2 alakasız", FeedbackTag.IRRELEVANT),
        ("2 alakasiz", FeedbackTag.IRRELEVANT),  # without diacritics
        ("2 ilgisiz", FeedbackTag.IRRELEVANT),
        ("2 arşivle", FeedbackTag.ARCHIVE),
        ("2 indir", FeedbackTag.ARCHIVE),
        ("2 sor", FeedbackTag.ASK),
        ("2 hocaya", FeedbackTag.ASK),
        ("2 IRRELEVANT", FeedbackTag.IRRELEVANT),
        ("2 #archive", FeedbackTag.ARCHIVE),
    ],
)
def test_turkish_and_english_synonyms_always_accepted(text: str, tag: FeedbackTag) -> None:
    (reply,) = parse(text, lang="en")
    assert reply.tag is tag and reply.items == ["2"]
    (reply_tr,) = parse(text, lang="tr")
    assert reply_tr.tag is tag


def test_multiple_numbers_and_connectors() -> None:
    (reply,) = parse("1,3, 5 and 7 good")
    assert reply.items == ["1", "3", "5", "7"]
    (reply_tr,) = parse("1 ve 2 oku")
    assert reply_tr.items == ["1", "2"] and reply_tr.tag is FeedbackTag.READ


def test_h_numbers_for_weekly_section() -> None:
    (reply,) = parse("H2 read")
    assert reply.items == ["H2"]
    (reply2,) = parse("h1,h3 archive")
    assert reply2.items == ["H1", "H3"]


def test_daily_and_weekly_never_mixed() -> None:
    (reply,) = parse("3, H2 good")
    assert reply.ambiguous  # the agent asks one question instead of guessing
    assert set(reply.ambiguous) >= {"3", "H2"}


def test_note_after_colon() -> None:
    (reply,) = parse("4 good: nice use of nodal modulation")
    assert reply.note == "nice use of nodal modulation"
    assert reply.items == ["4"]


def test_ask_with_target_and_note() -> None:
    (reply,) = parse("5 ask:Example Mentor")
    assert reply.tag is FeedbackTag.ASK and reply.ask_target == "Example"
    (reply2,) = parse("5 ask:mentor : about section 3")
    assert reply2.ask_target == "mentor" and reply2.note == "about section 3"
    (reply3,) = parse("5 sor:hoca")
    assert reply3.tag is FeedbackTag.ASK and reply3.ask_target == "hoca"
    (plain,) = parse("5 ask")
    assert plain.tag is FeedbackTag.ASK and plain.ask_target is None


def test_several_replies_per_message() -> None:
    replies = parse("1 good\n2,3 irrelevant; 4 read: later")
    assert [(r.items, r.tag) for r in replies] == [
        (["1"], FeedbackTag.GOOD),
        (["2", "3"], FeedbackTag.IRRELEVANT),
        (["4"], FeedbackTag.READ),
    ]
    assert replies[2].note == "later"


def test_tag_first_order_is_accepted() -> None:
    (reply,) = parse("good 3")
    assert reply.items == ["3"] and reply.tag is FeedbackTag.GOOD and not reply.ambiguous


def test_unknown_words_are_ambiguous_not_guessed() -> None:
    (reply,) = parse("3 maybe")
    assert reply.ambiguous == ["3", "maybe"] and reply.items == []
    (reply2,) = parse("good")
    assert reply2.items == [] and "<no item number>" in reply2.ambiguous
    assert parse("") == []
    assert parse("   \n ; ") == []


def test_markdown_tag_from_ticked_checkbox() -> None:
    assert markdown_tag("- [x] 3. A fictional title #good") is FeedbackTag.GOOD
    assert markdown_tag("- [X] 3. A fictional title -> irrelevant") is FeedbackTag.IRRELEVANT
    assert markdown_tag("- [x] 3. A fictional title -> oku") is FeedbackTag.READ
    assert markdown_tag("- [x] 3. A fictional title #ask:mentor") is FeedbackTag.ASK
    assert markdown_tag("- [ ] 3. A fictional title #good") is None  # not ticked
    assert markdown_tag("- [x] 3. A fictional title") is None  # no tag
    assert markdown_tag("- [x] 3. A title about #hashtags elsewhere") is None
