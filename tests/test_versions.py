from datetime import date

from proviso.index.chunking import article_chunks
from proviso.retrieve.versions import SpanMap, covers


class FakeConn:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *a, **k):
        return iter(self.rows)


def test_spanmap_picks_span_in_force():
    m = SpanMap(FakeConn([("법", "제1조", date(2000, 1, 1), date(2005, 1, 1)),
                          ("법", "제1조", date(2005, 1, 1), None)]))
    assert m.at("법", "제1조", date(2004, 12, 31)) == (date(2000, 1, 1), date(2005, 1, 1))
    assert m.at("법", "제1조", date(2005, 1, 1)) == (date(2005, 1, 1), None)
    assert m.at("법", "제1조", date(1999, 1, 1)) is None
    assert m.at("법", "제2조", date(2005, 1, 1)) is None


def test_spanmap_gap_after_repeal():
    m = SpanMap(FakeConn([("법", "제1조", date(2000, 1, 1), date(2001, 1, 1))]))
    assert m.at("법", "제1조", date(2002, 1, 1)) is None


def test_covers():
    assert covers("2000-01-01", None, date(2026, 1, 1))
    assert not covers("2000-01-01", "2001-01-01", date(2001, 1, 1))


def test_chunks_carry_spans():
    a = {"article_no": "제1조", "valid_from": date(2000, 1, 1), "valid_to": None, "text": "제1조(목적) 본문",
         "title": "목적", "heading": None, "doc_title": "법", "spans": ["2000-01-01", "2003-01-01"]}
    tok = lambda t: [(i, i + 1) for i in range(len(t))]
    c = article_chunks([a], tok, size=500, header=True)
    assert c[0]["articles"][0]["spans"] == ["2000-01-01", "2003-01-01"]


def test_period_match_ignores_span_split():
    from proviso.eval.metrics import Hit, matches
    from proviso.eval.schema import Gold

    g = Gold(doc_id="법", article="제1조", valid_from=date(2000, 1, 1))
    h = Hit(chunk_id="x", doc_id="법", article_no="제1조", valid_from=date(2003, 1, 1))
    period = {("법", "제1조", date(2000, 1, 1)): date(2000, 1, 1), ("법", "제1조", date(2003, 1, 1)): date(2000, 1, 1)}
    assert not matches(g, h, date(2026, 1, 1))
    assert matches(g, h, date(2026, 1, 1), period)
    period[("법", "제1조", date(2003, 1, 1))] = date(2003, 1, 1)
    assert not matches(g, h, date(2026, 1, 1), period)
