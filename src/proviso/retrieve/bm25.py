"""키워드 검색기(B1·B3): 조문 하나 = 문서 하나로 BM25 색인을 메모리에 만든다.

벡터 색인과 달리 조각으로 나눌 필요가 없다(입력 길이 한도가 없고, 긴 조문은 BM25 길이 정규화가 다룬다).
header=True면 2장의 문맥 머리말(법령명 > 장 > 조 제목)을 본문 앞에 붙여 함께 색인한다.
색인은 기준일(as_of, 2장 색인과 같은 날)에 유효한 조문만 담는다.
7장: versions=True면 제정 이후 모든 본문 기간으로 색인을 하나 더 만들고, 질문에 기준일이 있으면 그 색인에서
그날 시행 중이던 본문만 찾는다. 기준일이 없으면 현행 색인을 그대로 쓴다(현행 문항 결과는 바뀌지 않는다).
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from proviso.eval.metrics import Hit
from proviso.index.bm25 import BM25, TOKENIZERS
from proviso.index.build import current_articles, text_periods
from proviso.index.chunking import header_for


class BM25Retriever:
    name = "bm25"

    def __init__(self, conn, tokenizer: str = "morph", header: bool = True, as_of: str | date = "2026-11-20",
                 k1: float = 1.2, b: float = 0.75, articles: dict[str, list[dict]] | None = None,
                 versions: bool = False):
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
        self._versions = versions
        if versions:
            from proviso.retrieve.versions import SpanMap

            self._params["versions"] = "all"
            self._vdocs = [(d, a) for d, arts in text_periods(conn).items() for a in arts]
            self._vbm25 = BM25([self._tok((header_for(a) if header else "") + a["text"]) for _, a in self._vdocs],
                               k1=k1, b=b)
            self._spans = SpanMap(conn)
        self._build_seconds = round(time.perf_counter() - t0, 1)

    def describe(self) -> dict[str, Any]:
        return {"type": "bm25", **self._params, "docs": len(self._docs), "build_seconds": self._build_seconds,
                "uses_as_of": self._versions}

    def search(self, question: str, *, k: int, as_of: date | None = None) -> list[Hit]:
        if self._versions and as_of is not None:
            return self._search_at(question, k, as_of)
        hits = []
        for i, score in self._bm25.top(self._tok(question), k):
            doc_id, a = self._docs[i]
            hits.append(Hit(chunk_id=f"bm25#{i}", doc_id=doc_id, article_no=a["article_no"],
                            valid_from=a["valid_from"], valid_to=a["valid_to"], score=float(score)))
        return hits

    def _search_at(self, question: str, k: int, as_of: date) -> list[Hit]:
        from proviso.retrieve.versions import covers

        hits = []
        for i, score in sorted(self._vbm25.scores(self._tok(question)).items(), key=lambda x: (-x[1], x[0])):
            doc_id, a = self._vdocs[i]
            if not covers(a["valid_from"], a["valid_to"], as_of):
                continue
            span = self._spans.at(doc_id, a["article_no"], as_of)
            if span is None:
                continue
            hits.append(Hit(chunk_id=f"bm25v#{i}", doc_id=doc_id, article_no=a["article_no"],
                            valid_from=span[0], valid_to=span[1], score=float(score)))
            if len(hits) >= k:
                break
        return hits
