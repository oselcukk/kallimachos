from __future__ import annotations

import datetime as dt

from kallimachos.config.schema import RssConfig, RssFeed
from kallimachos.interfaces import HttpResponse, Ident, IdentKind, Record
from kallimachos.sources.rss import RssSource
from tests.unit.test_sources_fakes import Fail, FakeHttp, empty_profile, fixture, window

W = window("2026-10-11T00:00", "2026-10-18T00:00")
RSS2 = "https://journal.example.org/search.rss"
ATOM = "https://feeds.example.org/atom/math.NT"
EMPTY = "https://feeds.example.org/rss/math.NT"


def _config() -> RssConfig:
    return RssConfig(
        enabled=True,
        feeds=[
            RssFeed(name="example-journal", url=RSS2, channel_hint="A"),
            RssFeed(name="example-atom", url=ATOM),
            RssFeed(name="weekend", url=EMPTY),
        ],
    )


def _http() -> FakeHttp:
    return (
        FakeHttp()
        .add(RSS2, fixture("rss", "rss2"))
        .add(ATOM, fixture("rss", "atom"))
        .add(EMPTY, fixture("rss", "rss2_empty"))
    )


def test_discover_rss2_and_atom() -> None:
    src = RssSource(_config())
    recs = list(src.discover(W, empty_profile(), _http()))
    titles = [r.title for r in recs]
    assert titles == [
        "Congruences for toy L-functions",
        "Local obstructions in an invented setting",
        "An Atom entry about fictional modular curves",
    ]
    first = recs[0]
    assert first.doi == "10.0000/example.61"
    assert first.published == dt.date(2026, 10, 17)
    assert first.abstract and "fictional principle" in first.abstract
    assert first.queries == {"rss:example-journal", "channel_hint:A"}
    assert recs[1].abstract and "aX^2+bY^2" in recs[1].abstract  # JATS inside description kept
    atom = recs[2]
    assert atom.arxiv_id == "2610.00005" and atom.doi == "10.0000/example.64"
    assert [a.name for a in atom.authors] == ["Example Author A", "Example Author B"]
    assert atom.url == "https://arxiv.org/abs/2610.00005v1"
    assert src.hosts == ("feeds.example.org", "journal.example.org")


def test_rdf_rss1_and_undated_items() -> None:
    body = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
 xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">
 <channel rdf:about="https://journal.example.org/rss"><title>Example</title></channel>
 <item rdf:about="https://journal.example.org/a1">
  <title>An RDF item</title><link>https://journal.example.org/a1</link>
  <dc:creator>Example Author A</dc:creator><dc:date>2026-10-15</dc:date>
  <prism:doi>10.0000/example.65</prism:doi>
 </item>
 <item rdf:about="https://journal.example.org/a2"><title>Undated item</title>
  <link>https://journal.example.org/a2.pdf</link></item>
</rdf:RDF>"""
    cfg = RssConfig(feeds=[RssFeed(name="rdf", url=RSS2)])
    http = FakeHttp().add(RSS2, HttpResponse(url=RSS2, status=200, headers={}, body=body))
    recs = list(RssSource(cfg).discover(W, empty_profile(), http))
    assert [r.title for r in recs] == ["An RDF item", "Undated item"]
    assert recs[0].doi == "10.0000/example.65" and recs[0].authors[0].name == "Example Author A"
    assert recs[1].published is None


def test_bad_feed_is_error_and_others_continue() -> None:
    cfg = RssConfig(feeds=[RssFeed(name="broken", url=EMPTY), RssFeed(name="ok", url=RSS2)])
    http = FakeHttp().add(EMPTY, HttpResponse(url=EMPTY, status=200, headers={}, body=b"<html/>"))
    http.add(RSS2, fixture("rss", "rss2"))
    src = RssSource(cfg)
    assert len(list(src.discover(W, empty_profile(), http))) == 2
    assert src.errors == ["rss: feed broken is not RSS or Atom"]


def test_metadata_and_oa() -> None:
    src = RssSource(_config())
    assert src.metadata(Ident(IdentKind.DOI, "10.0000/x"), FakeHttp()) is None
    pdf = Record(title="t", url="https://journal.example.org/a2.pdf")
    assert [c.url for c in src.oa_pdf_candidates(pdf, FakeHttp())] == [pdf.url]
    assert src.oa_pdf_candidates(Record(title="t", url="https://x.example/a"), FakeHttp()) == []


def test_test() -> None:
    assert RssSource(_config()).test(_http()).ok
    assert not RssSource(RssConfig()).test(FakeHttp()).ok
    assert not RssSource(_config()).test(FakeHttp().add(RSS2, Fail(404))).ok


def test_clean_text_inline_and_block_tags() -> None:
    from kallimachos.sources.base import clean_text

    assert clean_text(
        "<jats:title>Abstract</jats:title><jats:p>Toy <i>L</i>-functions</jats:p>"
    ) == ("Abstract Toy L-functions")
    assert clean_text("a<br/>b &amp; c") == "a b & c"
