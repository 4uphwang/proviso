"""벡터 검색(D1): 질문 임베딩과 색인 조각 임베딩의 코사인 거리로 상위 k 조각을 찾는다.

조각 하나가 조문 여러 개에 걸치면(고정 길이 청킹) 조각 안 순서대로 조문을 모두 결과에 넣는다.
채점은 같은 조문 버전을 묶어 처음 나온 순위만 쓰므로, 순위는 '조문 기준'이 된다.
이 색인은 현행 조문만 담으므로 as_of는 쓰지 않는다(기준선의 한계. 7장에서 다룬다).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from proviso.eval.metrics import Hit


class DenseRetriever:
    name = "dense"

    def __init__(self, conn, index: str, embedder=None):
        from proviso.index.build import latest_index
        from proviso.index.embed import Embedder

        self._conn = conn
        self._index_id = latest_index(conn, index)
        if not self._index_id:
            raise ValueError(f"색인 {index!r}이 없음. 먼저 `proviso index build {index}`")
        method, n = conn.execute("SELECT method, chunk_count FROM index_versions WHERE index_id = %s",
                                 (self._index_id,)).fetchone()
        self._method = method
        self._chunks = n
        em = method["embedding"]
        self._embedder = embedder or Embedder(em["model"], max_length=em.get("max_length", 512))

    def describe(self) -> dict[str, Any]:
        return {"type": "dense", "index_id": self._index_id, "chunks": self._chunks, **self._method,
                "uses_as_of": False}

    def search(self, question: str, *, k: int, as_of: date | None = None) -> list[Hit]:
        q = self._embedder.encode([question])[0]
        qv = "[" + ",".join(f"{x:.6f}" for x in q) + "]"
        rows = self._conn.execute(
            "SELECT chunk_no, doc_id, articles, 1 - (embedding <=> %s::vector) FROM index_chunks"
            " WHERE index_id = %s ORDER BY embedding <=> %s::vector LIMIT %s",
            (qv, self._index_id, qv, k)).fetchall()
        hits = []
        for chunk_no, doc_id, arts, score in rows:
            for a in arts:
                hits.append(Hit(chunk_id=f"{self._index_id}#{chunk_no}", doc_id=doc_id, article_no=a["article_no"],
                                valid_from=date.fromisoformat(a["valid_from"]),
                                valid_to=date.fromisoformat(a["valid_to"]) if a["valid_to"] else None,
                                score=float(score)))
        return hits
