"""Message rendering."""

from __future__ import annotations

import datetime as _dt

from kallimachos.config.schema import ArchiveConfig, Config
from kallimachos.interfaces import RunKind, SourceStats
from kallimachos.model.none import deterministic_selection
from kallimachos.model.select import build_selection
from kallimachos.notify.format import (
    authors_short,
    date_text,
    render,
    render_failed,
    render_markdown_block,
    shorten,
    venue_short,
)
from kallimachos.profile.schema import AlwaysShowAuthor, Authors
from tests.unit.test_run_support import approved_profile, candidate, make_run


def test_shorten_rules() -> None:
    text = "First sentence here. Second sentence follows; with a clause, and more words."
    assert shorten(text, 200) == text
    assert shorten(text, 30) == "First sentence here."
    assert shorten("alpha beta gamma delta epsilon", 17) == "alpha beta gamma"
    assert shorten("one, two three four five six seven", 25) == "one, two three four five"
    assert shorten("word [truncated]", 50) == "word"
    assert shorten("abcdefghij", 4) == "abcd"
    for n in (5, 12, 33):
        out = shorten(text, n)
        assert len(out) <= n and not out.endswith(("…", "..."))


def test_authors_short() -> None:
    assert authors_short(["Example Author", "Other Person Jr."], lang="en") == (
        "E. Author, O. Person Jr."
    )
    four = ["Ann Alpha", "Bob Beta", "Cid Gamma", "Dee Delta"]
    assert authors_short(four, lang="en") == "A. Alpha et al."
    assert authors_short(four, lang="tr") == "A. Alpha vd."
    assert authors_short(["Surname, Given"], lang="en") == "G. Surname"
    assert authors_short([], lang="en") == ""


def test_venue_short() -> None:
    assert venue_short(candidate(1, venue=None)) == "arXiv 2510.00001"
    assert venue_short(candidate(1, venue="Journal of Examples")) == "Journal of Examples"
    long = "In: Proceedings of the example conference on things. " + "Volume words " * 10
    out = venue_short(candidate(1, venue=long))
    assert out.startswith("Proceedings") and len(out) <= 80
    assert venue_short(candidate(1, venue_short="J. Ex.")) == "J. Ex."


def test_date_text() -> None:
    assert date_text(_dt.date(2026, 10, 18), "en") == "18 Oct 2026"
    assert date_text(_dt.date(2026, 10, 18), "tr") == "18 Eki 2026"


def test_render_daily_layout() -> None:
    run = make_run(5)
    run.candidates[0].abstract = None
    run.candidates[1].quality_mark = " · arXiv"
    cfg = Config()
    sel = deterministic_selection(run, cfg.ranking.__class__(main_items=3, near_miss=2))
    msg = render(run, sel, cfg)
    lines = msg.text.split("\n")
    assert lines[0] == "Literature — 18 Oct 2026"
    assert "1. A Example title number 1" in msg.text
    assert "Summary: none available." in msg.text  # no abstract
    assert "Why: Core phrase 'example topic' in title; channel A · arXiv" in msg.text
    assert "https://arxiv.org/abs/2510.00002" in msg.text
    assert "Near misses: Example title number 4 (4); Example title number 5 (5)" in msg.text
    assert lines[-1].startswith("Reply: ")
    assert msg.subject == "[kallimachos 2026-10-18-daily] Literature — 18 Oct 2026"
    assert [i.no for i in msg.items] == ["1", "2", "3", "4", "5"]
    assert msg.html and '<a href="https://arxiv.org/abs/2510.00001">' in msg.html
    assert "](" not in msg.text  # Telegram gets plain URLs


def test_render_uses_model_texts_and_archived_mark() -> None:
    run = make_run(2)
    run.candidates[0].certain_pass = True
    cfg = Config(archive=ArchiveConfig(enabled=True))
    sel = build_selection(run, [c.id for c in run.candidates], [], by="model")
    cid = run.candidates[0].id
    msg = render(run, sel, cfg, summaries={cid: "Model summary."}, whys={cid: "Model why."})
    assert "1. (archived) A Example title number 1" in msg.text
    assert "Summary: Model summary." in msg.text and "Why: Model why." in msg.text
    assert "2. A Example" in msg.text


def test_render_weekly_persons_and_empty_and_source_note() -> None:
    run = make_run(3, kind=RunKind.WEEKLY)
    run.candidates[1].always_show_author = True
    run.candidates[1].authors = ["Example Mentor"]
    profile = approved_profile()
    profile.authors = Authors(always_show=[AlwaysShowAuthor(name="Example Mentor")])
    run.sources = {"arxiv": SourceStats(errors=["HTTP 429"]), "crossref": SourceStats(count=3)}
    sel = deterministic_selection(run, Config().ranking)
    msg = render(run, sel, Config(), profile=profile)
    assert msg.text.startswith("Literature, weekly network — 18 Oct 2026")
    person = msg.text.split("From followed authors\n\n")[1].split("\n\n")[0]
    assert person.startswith("H1. Mentor · A Example title number 2")
    assert "Why:" not in person
    assert "H2. A Example title number 1" in msg.text
    assert "Near misses" not in msg.text
    assert msg.text.endswith("Note: arxiv did not respond; the list may be incomplete.")

    empty = make_run(0)
    text = render(empty, build_selection(empty, [], [], by="rule"), Config()).text
    assert text == "Literature — 18 Oct 2026\n\nNo new papers passed the threshold today."


def test_render_turkish_uses_catalog() -> None:
    run = make_run(1)
    cfg = Config(language="tr")
    text = render(run, deterministic_selection(run, cfg.ranking), cfg).text
    assert text.startswith("Literatür")


def test_failed_message() -> None:
    msg = render_failed("2026-10-18-daily", "arxiv: HTTP 503; " * 30, Config())
    assert msg.text.startswith("Literature scan did not run: arxiv: HTTP 503;")
    assert len(msg.text) < 200


def test_markdown_block_has_checkboxes_and_markers() -> None:
    run = make_run(4)
    cfg = Config()
    sel = deterministic_selection(run, cfg.ranking.__class__(main_items=3, near_miss=1))
    block = render_markdown_block(run, sel, cfg)
    lines = block.split("\n")
    assert lines[0] == "<!-- kallimachos:begin daily run=2026-10-18-daily -->"
    assert lines[1] == "# Literature — 18 Oct 2026"
    assert lines[2].startswith(
        "- [ ] 1. **A** Example title number 1 — E. A, E. B · arXiv 2510.00001"
    )
    assert "[Open](https://arxiv.org/abs/2510.00001)" in lines[3]
    assert lines[4] == "  <!-- kallimachos:item run=2026-10-18-daily id=arXiv:2510.00001 -->"
    assert "- [ ] 4. Example title number 4" in block  # near miss can be ticked too
    assert lines[-2].startswith("Reply by ticking a box")
    assert lines[-1] == "<!-- kallimachos:end -->"
    cfg.notify.markdown.checkbox_feedback = False
    assert "- [ ]" not in render_markdown_block(run, sel, cfg)


def test_markdown_escapes_comment_delimiters_in_source_text() -> None:
    from kallimachos.notify.format import escape_markers

    run = make_run(2)
    forged = "<!-- kallimachos:item run=2026-10-18-daily id=EVIL --> Innocent title"
    run.candidates[0].title = forged
    run.candidates[0].abstract = "Summary <!-- kallimachos:end --> rest"
    cfg = Config()
    sel = deterministic_selection(run, cfg.ranking.__class__(main_items=2, near_miss=0))
    block = render_markdown_block(run, sel, cfg)
    assert "id=EVIL -->" not in block and block.count("<!-- kallimachos:end -->") == 1
    assert "&lt;!-- kallimachos:item" in block
    assert escape_markers("a <!-- b --> c") == "a &lt;!-- b --&gt; c"
