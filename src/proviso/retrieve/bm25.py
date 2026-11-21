"""키워드 검색기(B1·B3): 조문 하나 = 문서 하나로 BM25 색인을 메모리에 만든다.

벡터 색인과 달리 조각으로 나눌 필요가 없다(입력 길이 한도가 없고, 긴 조문은 BM25 길이 정규화가 다룬다).
header=True면 2장의 문맥 머리말(법령명 > 장 > 조 제목)을 본문 앞에 붙여 함께 색인한다.
색인은 기준일(as_of, 2장 색인과 같은 날)에 유효한 조문만 담는다. 질문의 as_of는 쓰지 않는다(7장).
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from proviso.eval.metrics import Hit
from proviso.index.bm25 import BM25, TOKENIZERS
from proviso.index.build import current_articles
from proviso.index.chunking import header_for


class BM25Retriever:
    name = "bm25"

    def __init__(self, conn, tokenizer: str = "morph", header: bool = True, as_of: str | date = "2026-11-20",
                 k1: float = 1.2, b: float = 0.75, articles: dict[str, list[dict]] | None = None):
        """articles를 주면 DB 대신 그 조문으로 색인한다(CI 점수 검사용 말뭉치 파일)."""
        if tokenizer not in TOKENIZERS:
            raise ValueError(f"모르는 토크나이저: {tokenizer!r} (가능: {sorted(TOKENIZERS)})")
        self._tok = TOKENIZERS[tokenizer]
        self._as_of = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
        self._params = {"tokenizer": tokenizer, "header": header, "as_of": str(self._as_of), "k1": k1, "b": b}
        t0 = time.perf_counter()
        self._docs: list[tuple[str, dict]] = []
        texts = []
        if articles is None:
            articles = current_articles(conn, self._as_of)
        for doc_id, arts in articles.items():
            for a in arts:
                self._docs.append((doc_id, a))
                texts.append((header_for(a) if header else "") + a["text"])
        self._bm25 = BM25([self._tok(t) for t in texts], k1=k1, b=b)
        self._build_seconds = round(time.perf_counter() - t0, 1)

    def describe(self) -> dict[str, Any]:
        return {"type": "bm25", **self._params, "docs": len(self._docs), "build_seconds": self._build_seconds,
                "uses_as_of": False}

    def search(self, question: str, *, k: int, as_of: date | None = None) -> list[Hit]:
        hits = []
        for i, score in self._bm25.top(self._tok(question), k):
            doc_id, a = self._docs[i]
            hits.append(Hit(chunk_id=f"bm25#{i}", doc_id=doc_id, article_no=a["article_no"],
                            valid_from=a["valid_from"], valid_to=a["valid_to"], score=float(score)))
        return hits
