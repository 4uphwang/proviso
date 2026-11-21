"""재정렬(R1), 질의 가공(Q1·Q2), 그래프 확장(G1)의 순서 규칙. 모델 대신 가짜 점수기·LLM을 쓴다."""

from datetime import date

from proviso.eval.metrics import Hit
from proviso.retrieve.query_llm import LLMQueryTransform
from proviso.retrieve.rerank import RerankRetriever

D = date(2020, 1, 1)


class Fixed:
    def __init__(self, arts):
        self.arts = arts
        self.seen = []

    def describe(self):
        return {}

    def search(self, q, *, k, as_of=None):
        self.seen.append(q)
        return [Hit(f"c{i}", "법", a, D) for i, a in enumerate(self.arts[:k])]


def make_reranker(inner, texts, scorer, top_n):
    r = RerankRetriever.__new__(RerankRetriever)
    r._inner, r._top_n, r._batch, r._scorer, r._params = inner, top_n, 8, scorer, {}
    r._skip, r.reranked = None, []
    r._texts = texts
    return r


def test_rerank_uses_best_chunk_and_keeps_tail_order():
    inner = Fixed(["제1조", "제2조", "제3조", "제4조"])
    texts = {("법", "제1조", str(D)): ["무관"], ("법", "제2조", str(D)): ["무관", "정답 조각"],
             ("법", "제3조", str(D)): ["조금"], ("법", "제4조", str(D)): ["정답 조각"]}
    score = {"무관": 0.0, "조금": 0.5, "정답 조각": 1.0}

    def scorer(pairs, batch_size):
        return [score[t] for _, t in pairs]

    hits = make_reranker(inner, texts, scorer, top_n=3).search("q", k=4)
    # 상위 3개만 다시 매긴다: 제2조(조각 최댓값 1.0) > 제3조 > 제1조, 제4조는 4위 그대로
    assert [h.article_no for h in hits] == ["제2조", "제3조", "제1조", "제4조"]


class FakeLLM:
    model = "fake"
    options = {}
    last_seconds = 0.1

    def generate(self, prompt):
        return "법률 용어"


def test_query_transform_keeps_original_question():
    inner = Fixed(["제1조"])
    t = LLMQueryTransform(inner, mode="rewrite", client=FakeLLM())
    t.search("카드 잃어버림", k=1)
    assert inner.seen == ["카드 잃어버림\n법률 용어"]
    assert t.describe()["query_llm"]["mode"] == "rewrite"


def test_route_skips_rerank_when_first_stage_is_confident():
    class Scored(Fixed):
        def search(self, q, *, k, as_of=None):
            return [Hit(f"c{i}", "법", a, D, score=s) for i, (a, s) in enumerate(self.arts[:k])]

    texts = {("법", "제1조", str(D)): ["무관"], ("법", "제2조", str(D)): ["정답"]}

    def scorer(pairs, batch_size):
        return [1.0 if t == "정답" else 0.0 for _, t in pairs]

    sure = make_reranker(Scored([("제1조", 0.04), ("제2조", 0.01)]), texts, scorer, top_n=2)
    sure._skip, sure.reranked = 0.03, []
    assert [h.article_no for h in sure.search("q", k=2)] == ["제1조", "제2조"] and sure.reranked == [False]
    unsure = make_reranker(Scored([("제1조", 0.02), ("제2조", 0.01)]), texts, scorer, top_n=2)
    unsure._skip, unsure.reranked = 0.03, []
    assert [h.article_no for h in unsure.search("q", k=2)] == ["제2조", "제1조"] and unsure.reranked == [True]
