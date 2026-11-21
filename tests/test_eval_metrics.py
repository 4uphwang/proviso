from datetime import date

import pytest

from proviso.eval.compare import CompareError, compare_runs
from proviso.eval.metrics import (Hit, bootstrap_ci, dedupe, item_values, paired_diff_ci, score_item)
from proviso.eval.run import run_items, summarize_run
from proviso.eval.schema import EvalItem

TODAY = date(2026, 11, 20)


def item(iid="r1", category="lookup", gold=None, as_of=None, tags=(), style="keyword"):
    gold = gold or [{"doc_id": "전자금융거래법", "article": "제9조"}]
    return EvalItem.model_validate({"id": iid, "set": "retrieval", "split": "dev", "question": f"질문 {iid}",
                                    "category": category, "answerable": True, "gold": gold, "as_of": as_of,
                                    "tags": list(tags), "origin": "human", "style": style})


def hit(article, valid_from=date(2020, 1, 1), valid_to=None, doc="전자금융거래법", part=0):
    return Hit(chunk_id=f"{doc}:{article}@{valid_from}#{part}", doc_id=doc, article_no=article,
               valid_from=valid_from, valid_to=valid_to)


def test_rank_counts_articles_not_chunks():
    # 같은 조문 조각 3개가 앞을 채워도 정답 순위는 조문 단위로 센다
    hits = [hit("제2조", part=0), hit("제2조", part=1), hit("제2조", part=2), hit("제9조")]
    assert len(dedupe(hits)) == 2
    assert score_item(item(), hits, TODAY)["rank"] == 2


def test_current_question_rejects_old_version():
    old = hit("제9조", date(2015, 1, 1), date(2020, 1, 1))
    cur = hit("제9조", date(2020, 1, 1))
    assert score_item(item(), [old, cur], TODAY)["rank"] == 2


def test_temporal_question_needs_that_version():
    it = item(category="temporal", as_of="2017-06-01",
              gold=[{"doc_id": "전자금융거래법", "article": "제9조", "valid_from": "2015-01-01"}])
    old = hit("제9조", date(2015, 1, 1), date(2020, 1, 1))
    cur = hit("제9조", date(2020, 1, 1))
    assert score_item(it, [cur, old], TODAY)["rank"] == 2
    assert score_item(it, [cur], TODAY)["rank"] is None


def test_cross_ref_tracks_law_article_separately():
    it = item(category="cross_ref", tags=["위임"], gold=[
        {"doc_id": "전자금융거래법 시행령", "article": "제5조"}, {"doc_id": "전자금융거래법", "article": "제9조"}])
    s = score_item(it, [hit("제9조"), hit("제5조", doc="전자금융거래법 시행령")], TODAY)
    assert s == {"rank": 2, "rank_law": 1}


def test_item_values():
    assert item_values(3) == {"recall@1": 0, "recall@5": 1, "recall@10": 1, "recall@20": 1, "mrr@10": pytest.approx(1 / 3)}
    assert item_values(15)["mrr@10"] == 0 and item_values(15)["recall@20"] == 1
    assert item_values(None) == {"recall@1": 0, "recall@5": 0, "recall@10": 0, "recall@20": 0, "mrr@10": 0}


def test_bootstrap_is_deterministic_and_brackets_mean():
    vals = [1.0] * 70 + [0.0] * 30
    lo, hi = bootstrap_ci(vals)
    assert lo < 0.7 < hi and (lo, hi) == bootstrap_ci(vals)
    assert bootstrap_ci([1.0] * 50) == (1.0, 1.0)


def test_paired_diff_detects_consistent_gain():
    a = [0.0] * 50 + [1.0] * 50
    b = [1.0] * 70 + [1.0] * 30  # 앞 50문항 중 대부분 새로 맞힘
    d, lo, hi = paired_diff_ci(a, b)
    assert d == pytest.approx(0.5) and lo > 0
    d, lo, hi = paired_diff_ci(a, a)
    assert (d, lo, hi) == (0, 0, 0)


class FakeRetriever:
    name = "fake"

    def __init__(self, answers):
        self.answers = answers

    def describe(self):
        return {"type": "fake"}

    def search(self, question, *, k, as_of=None):
        return self.answers.get(question, [])[:k]


def _run(items, answers, run_id="A", snapshot="s1", hash_="h1"):
    per_item = run_items(FakeRetriever(answers), items, k=20, today=TODAY, log=lambda *_: None)
    return {"run_id": run_id, "snapshot_id": snapshot, "evalset": {"hash": hash_, "split": "dev", "path": "x"},
            "summary": summarize_run(items, per_item, n_boot=100), "per_item": per_item}


def test_run_and_summary_by_cell():
    items = [item("r1", style="keyword"), item("r2", style="casual"),
             item("r3", tags=["별표"], gold=[{"doc_id": "전자금융거래법", "article": "별표 1"}])]
    answers = {"질문 r1": [hit("제9조")], "질문 r2": [hit("제1조")] * 3 + [hit("제9조")], "질문 r3": []}
    run = _run(items, answers)
    s = run["summary"]
    assert s["overall"]["recall@1"]["mean"] == pytest.approx(1 / 3)
    assert s["overall"]["recall@5"]["mean"] == pytest.approx(2 / 3)  # r2는 묶은 뒤 2위
    assert set(s["by"]["유형"]) == {"lookup", "appendix"}
    assert s["by"]["형태"]["keyword"]["recall@1"]["mean"] == pytest.approx(0.5)


def test_compare_refuses_different_evalset_or_snapshot(monkeypatch):
    items = [item("r1")]
    monkeypatch.setattr("proviso.eval.compare.load_items", lambda *a, **k: items)
    a = _run(items, {"질문 r1": []})
    with pytest.raises(CompareError, match="평가셋"):
        compare_runs(a, _run(items, {}, hash_="h2"))
    with pytest.raises(CompareError, match="스냅샷"):
        compare_runs(a, _run(items, {}, snapshot="s2"))
    md = compare_runs(a, _run(items, {"질문 r1": [hit("제9조")]}, run_id="B"), n_boot=50)
    assert "상위 10 안에 들게 된 문항 1개" in md
