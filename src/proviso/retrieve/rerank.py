"""재정렬(R1): 앞 단계 검색 결과 상위 top_n개 조문을 cross-encoder로 다시 매긴다.

cross-encoder는 질문과 조문을 한 입력으로 같이 읽어 관련도를 낸다. 질문과 조문을 따로 임베딩하는 벡터 검색보다
정확하지만 느려서, 후보를 줄인 뒤에만 쓴다. 조문 본문은 2장 색인(머리말 포함 조각)의 텍스트를 쓰고,
여러 조각으로 나뉜 긴 조문은 조각 점수 중 가장 높은 값을 조문 점수로 한다.
top_n 밖의 결과는 앞 단계 순서 그대로 뒤에 붙인다.

라우팅(skip_score): 재정렬은 문항당 수 초가 걸린다. 앞 단계 1위의 점수가 skip_score 이상이면 재정렬을 건너뛴다.
RRF(검색기 2개, k_rrf 60)에서 2/61은 "두 검색기가 같은 조문을 모두 1위로 냈다"는 뜻이다. 결과를 보고 고르지 않도록
이 규칙 하나만 미리 정해 잰다.
"""

from __future__ import annotations

import time
from datetime import date
from functools import lru_cache
from typing import Any

from proviso.eval.metrics import Hit


@lru_cache(maxsize=2)
def load_cross_encoder(model: str, max_length: int):
    from sentence_transformers import CrossEncoder

    from proviso.index.embed import _device

    import torch

    dev = _device()
    dtype = torch.float16 if dev == "mps" else torch.float32  # 맥 GPU에서는 반정밀도(점수 순서는 거의 같고 더 빠르다)
    return CrossEncoder(model, device=dev, max_length=max_length, model_kwargs={"torch_dtype": dtype})


class RerankRetriever:
    name = "rerank"

    def __init__(self, conn, inner, *, index: str, model: str = "BAAI/bge-reranker-v2-m3", top_n: int = 30,
                 max_length: int = 512, batch_size: int = 16, scorer=None, skip_score: float | None = None):
        from proviso.index.build import latest_index

        self._inner = inner
        self._top_n = top_n
        self._batch = batch_size
        index_id = latest_index(conn, index)
        if not index_id:
            raise ValueError(f"색인 {index!r}이 없음")
        self._texts: dict[tuple, list[str]] = {}
        for doc_id, arts, text in conn.execute(
                "SELECT doc_id, articles, text FROM index_chunks WHERE index_id = %s ORDER BY chunk_no", (index_id,)):
            for a in arts:
                self._texts.setdefault((doc_id, a["article_no"], a["valid_from"]), []).append(text)
        self._scorer = scorer or load_cross_encoder(model, max_length).predict
        self._skip = skip_score
        self.reranked: list[bool] = []
        self._params = {"model": model, "top_n": top_n, "max_length": max_length, "index_id": index_id,
                        "skip_score": skip_score}

    def describe(self) -> dict[str, Any]:
        return {"type": "rerank", **self._params, "inner": self._inner.describe()}

    def search(self, question: str, *, k: int, as_of: date | None = None) -> list[Hit]:
        hits = self._inner.search(question, k=max(k, self._top_n), as_of=as_of)
        if self._skip is not None and hits and hits[0].score >= self._skip - 1e-12:
            self.reranked.append(False)
            return hits[:k]
        self.reranked.append(True)
        head, tail = hits[:self._top_n], hits[self._top_n:]
        pairs, owner = [], []
        for i, h in enumerate(head):
            for t in self._texts.get((h.doc_id, h.article_no, str(h.valid_from)), []):
                pairs.append((question, t))
                owner.append(i)
        best = [float("-inf")] * len(head)
        if pairs:
            # 길이가 비슷한 것끼리 묶어 채움(padding)을 줄인다
            idx = sorted(range(len(pairs)), key=lambda j: len(pairs[j][1]))
            scores = self._scorer([pairs[j] for j in idx], batch_size=self._batch)
            for j, s in zip(idx, scores):
                best[owner[j]] = max(best[owner[j]], float(s))
        order = sorted(range(len(head)), key=lambda i: (-best[i], i))
        out = [Hit(chunk_id=head[i].chunk_id, doc_id=head[i].doc_id, article_no=head[i].article_no,
                   valid_from=head[i].valid_from, valid_to=head[i].valid_to, score=best[i]) for i in order]
        return (out + tail)[:k]
