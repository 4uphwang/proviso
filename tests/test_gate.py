"""CI 점수 검사: 모델·DB 없이 형태소 BM25 + 약칭 확장으로 recall@10이 기준값 − 허용폭 아래로 떨어지면 실패."""

from proviso.eval.gate import CORPUS, check, load_corpus


def test_corpus_has_source_and_articles():
    meta, arts = load_corpus(CORPUS)
    assert "법제처" in meta["source"]
    assert sum(len(v) for v in arts.values()) > 1000


def test_recall_does_not_regress():
    ok, msg = check()
    assert ok, msg
